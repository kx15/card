#!/usr/bin/env python3
"""Build a vCard QR kit from contact.json.

Outputs
  site/index.html             the hosted card page (photo inlined, no requests)
  site/<slug>.vcf             the contact file its Save button downloads
  site/vercel.json            serves .vcf as text/vcard (needed on iOS)
  qr/vcard-direct.{png,svg}   QR with the contact embedded, no hosting needed
  qr/card-page.{png,svg}      QR pointing at card_url

Pillow is optional. With it, the headshot is square-cropped and downsized;
without it the original file is embedded as-is.
"""

import base64
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import segno

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

ROOT = Path(__file__).parent
# "docs" because GitHub Pages can only serve the repo root or /docs.
SITE = ROOT / "docs"
QR = ROOT / "qr"

# IPS design system, per ips-media-crm/src/app/globals.css
IPS_BLUE = "#1E22AA"

PHOTO_PX = 400          # square edge of the embedded headshot
PHOTO_QUALITY = 82

GLYPH = (
    '<svg class="glyph" width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">'
    '<path d="M4.5 2.5 9.5 7l-5 4.5" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

warnings = []


# ---------------------------------------------------------------- vCard

def esc(value):
    """Escape a vCard text value (RFC 6350 s3.4)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line):
    """Fold a long line at 75 octets; continuations start with one space."""
    if len(line) <= 75:
        return line
    parts, rest = [line[:75]], line[75:]
    while rest:
        parts.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(parts)


def tel_href(value):
    return "tel:" + re.sub(r"[^\d+]", "", value)


def with_scheme(value):
    return value if value.startswith(("http://", "https://")) else "https://" + value


def channel_href(ch):
    kind, value = ch["kind"], ch["value"]
    if kind == "call":
        return tel_href(value)
    if kind == "sms":
        return "sms:" + re.sub(r"[^\d+]", "", value)
    if kind == "whatsapp":
        return "https://wa.me/" + re.sub(r"[^\d]", "", value)
    if kind == "email":
        return "mailto:" + value
    return with_scheme(value)


def build_vcard(cfg, photo, *, minimal):
    """minimal=True trims the payload so the direct QR stays easy to scan."""
    name = cfg["name"]
    given, family = name.get("given", ""), name.get("family", "")
    full = " ".join(p for p in (given, family) if p)

    lines = [
        "BEGIN:VCARD",
        "VERSION:3.0",
        f"N:{esc(family)};{esc(given)};;;",
        f"FN:{esc(full)}",
    ]

    org, dept = cfg.get("org", ""), cfg.get("department", "")
    if org:
        lines.append(f"ORG:{esc(org)};{esc(dept)}" if dept else f"ORG:{esc(org)}")

    if cfg.get("role"):
        lines.append(f"TITLE:{esc(cfg['role'])}")

    for ch in cfg["channels"]:
        kind, value = ch["kind"], ch["value"]
        if kind in ("call", "sms", "whatsapp"):
            lines.append(f"TEL;TYPE={ch.get('vcard_type', 'CELL')},VOICE:{esc(value)}")
        elif kind == "email":
            lines.append(f"EMAIL;TYPE=INTERNET,WORK:{esc(value)}")
        elif kind == "website":
            lines.append(f"URL:{esc(with_scheme(value))}")
        elif not minimal:
            url = esc(with_scheme(value))
            # X-SOCIALPROFILE is an Apple extension: iOS files it under the
            # contact's social profiles, but Google Contacts drops it. The
            # plain URL is the fallback every client renders.
            lines.append(f"X-SOCIALPROFILE;TYPE={kind}:{url}")
            lines.append(f"URL:{url}")

    if not minimal:
        adr = cfg.get("address") or {}
        if adr.get("street"):
            lines.append(
                "ADR;TYPE=WORK:;;"
                f"{esc(adr.get('street', ''))};"
                f"{esc(adr.get('locality', ''))};;"
                f"{esc(adr.get('postcode', ''))};"
                f"{esc(adr.get('country', ''))}"
            )
        if cfg.get("note"):
            lines.append(f"NOTE:{esc(cfg['note'])}")
        if not str(cfg.get("card_url", "")).startswith("REPLACE_ME"):
            lines.append(f"URL:{esc(cfg['card_url'])}")

        # Photo goes in the file only. In the QR it would wreck scannability.
        if photo:
            lines.append(
                fold(f"PHOTO;ENCODING=b;TYPE={photo['vcard_type']}:{photo['b64']}")
            )

    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------- photo

def load_photo(cfg):
    """Return {'b64', 'mime', 'vcard_type'} for the headshot, or None."""
    raw = (cfg.get("photo") or "").strip()
    if not raw:
        return None

    src = Path(raw)
    if not src.is_absolute():
        src = ROOT / src

    if not src.exists():
        sys.exit(f"photo not found: {src}\nSet 'photo' in contact.json to a real file.")

    if HAVE_PIL:
        import io

        with Image.open(src) as im:
            im = im.convert("RGB")
            edge = min(im.size)
            left = (im.width - edge) // 2
            top = (im.height - edge) // 3   # bias upward; heads sit high in frame
            im = im.crop((left, top, left + edge, top + edge))
            im = im.resize((PHOTO_PX, PHOTO_PX), Image.LANCZOS)

            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=PHOTO_QUALITY, optimize=True)
            data = buf.getvalue()

        mime, vtype = "image/jpeg", "JPEG"
    else:
        data = src.read_bytes()
        ext = src.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        vtype = "PNG" if ext == ".png" else "JPEG"
        warnings.append(
            "Pillow not installed, so the headshot is embedded uncropped at "
            f"{len(data) // 1024} KB. For a square crop and a smaller file: "
            "pip install pillow"
        )

    return {"b64": base64.b64encode(data).decode("ascii"), "mime": mime,
            "vcard_type": vtype}


# ---------------------------------------------------------------- page

def build_page(cfg, vcf_name, photo):
    name = cfg["name"]
    given, family = name.get("given", ""), name.get("family", "")
    full = " ".join(p for p in (given, family) if p)

    name_lines = "\n".join(
        f'        <span class="line">{html.escape(part)}</span>'
        for part in (given, family)
        if part
    )

    org = cfg.get("org", "")
    needle = cfg.get("org_highlight") or ""
    if needle and needle in org:
        before, _, after = org.partition(needle)
        org_html = (
            html.escape(before)
            + f'<span class="mark">{html.escape(needle)}</span>'
            + html.escape(after)
        )
    else:
        if needle:
            warnings.append(
                f'org_highlight "{needle}" does not appear in org '
                f'"{org}", so nothing is emphasised. Use a substring of org.'
            )
        org_html = html.escape(org)

    if photo:
        photo_block = (
            f'      <img class="photo" src="data:{photo["mime"]};base64,{photo["b64"]}"'
            f' alt="{html.escape(full, quote=True)}" width="72" height="72">'
        )
    else:
        initials = "".join(p[0] for p in (given, family) if p).upper()
        photo_block = (
            f'      <div class="monogram" aria-hidden="true">{html.escape(initials)}</div>'
        )

    dept = cfg.get("department", "")
    dept_block = (
        f'    <p class="dept reveal" style="--d: 155ms">{html.escape(dept)}</p>'
        if dept else ""
    )

    rows = []
    for ch in cfg["channels"]:
        rows.append(
            "      <li>\n"
            f'        <a class="row" href="{html.escape(channel_href(ch), quote=True)}">\n'
            f'          <span class="label">{html.escape(ch["label"])}</span>\n'
            f'          <span class="value">{html.escape(ch["value"])}</span>\n'
            f"          {GLYPH}\n"
            "        </a>\n"
            "      </li>"
        )

    tz = cfg["timezone"]
    offset = tz["utc_offset_hours"]
    role = cfg.get("role", "")

    subs = {
        "TITLE": f"{full} — {role}" if role else full,
        "DESCRIPTION": " · ".join(p for p in (full, role, dept, org) if p),
        "NAME_LINES": name_lines,
        "PHOTO_BLOCK": photo_block,
        "DEPT_BLOCK": dept_block,
        "ROLE": html.escape(role),
        "ORG_HTML": org_html,
        "CHANNELS": "\n".join(rows),
        "VCF_FILE": html.escape(vcf_name, quote=True),
        "VCF_DOWNLOAD": html.escape(f"{full}.vcf", quote=True),
        "TZ_LABEL": html.escape(tz["label"]),
        "TZ_OFFSET": str(offset),
        "TZ_SIGNED": f"+{offset}" if offset >= 0 else str(offset),
        "HOURS_START": str(tz["hours_start"]),
        "HOURS_END": str(tz["hours_end"]),
        "HOURS_LABEL": f"{tz['hours_start']:02d}:00–{tz['hours_end']:02d}:00, Mon–Fri",
    }

    page = (ROOT / "template.html").read_text(encoding="utf-8")
    for key, value in subs.items():
        page = page.replace("{{" + key + "}}", value)

    left = re.findall(r"\{\{([A-Z_]+)\}\}", page)
    if left:
        sys.exit(f"template placeholder never filled: {sorted(set(left))}")

    return page


# ---------------------------------------------------------------- QR

def write_qr(payload, stem, *, error):
    qr = segno.make(payload, error=error)
    qr.save(str(QR / f"{stem}.png"), scale=12, border=4, dark=IPS_BLUE, light="#FFFFFF")
    qr.save(str(QR / f"{stem}.svg"), scale=12, border=4, dark=IPS_BLUE, light="#FFFFFF")

    modules = qr.symbol_size(scale=1, border=4)[0]
    return qr.version, modules, max(25, round(modules * 0.55))


# ---------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / "contact.json").read_text(encoding="utf-8"))

    SITE.mkdir(exist_ok=True)
    QR.mkdir(exist_ok=True)

    photo = load_photo(cfg)
    slug = cfg["slug"]
    vcf_name = f"{slug}.vcf"

    (SITE / vcf_name).write_bytes(build_vcard(cfg, photo, minimal=False).encode("utf-8"))
    (SITE / "index.html").write_text(build_page(cfg, vcf_name, photo), encoding="utf-8")

    (SITE / "vercel.json").write_text(
        json.dumps(
            {
                "headers": [
                    {
                        "source": "/(.*).vcf",
                        "headers": [
                            {"key": "Content-Type", "value": "text/vcard; charset=utf-8"},
                            {"key": "Content-Disposition",
                             "value": f'attachment; filename="{slug}.vcf"'},
                        ],
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    # CNAME is opt-in only. Writing one claims the hostname for THIS repo and
    # can break a site already served from that domain, so it is never derived
    # from card_url. Set "pages_custom_domain" explicitly to emit one.
    domain = str(cfg.get("pages_custom_domain", "")).strip()
    if domain:
        (SITE / "CNAME").write_text(domain + "\n", encoding="utf-8")
    else:
        (SITE / "CNAME").unlink(missing_ok=True)

    page_kb = len((SITE / "index.html").read_bytes()) // 1024
    print(f"docs/index.html   {page_kb} KB, {'photo inlined' if photo else 'monogram placeholder'}")
    print(f"docs/{vcf_name}       {len((SITE / vcf_name).read_bytes()) // 1024} KB")
    print("docs/vercel.json  serves .vcf as text/vcard (Vercel only, ignored by Pages)")
    print("docs/.nojekyll    stops Pages hiding files that start with _")
    print()

    direct = build_vcard(cfg, photo, minimal=True)
    v, mods, mm = write_qr(direct, "vcard-direct", error="m")
    print(f"qr/vcard-direct   v{v}, {mods}x{mods} modules, print >= {mm}mm wide")
    print(f"                  payload {len(direct)} chars, ECC medium")

    url = str(cfg.get("card_url", ""))
    if url.startswith("REPLACE_ME") or not url:
        print()
        print("qr/card-page      SKIPPED - card_url is not set.")
        for stale in ("card-page.png", "card-page.svg"):
            (QR / stale).unlink(missing_ok=True)
    else:
        v, mods, mm = write_qr(url, "card-page", error="h")
        print()
        print(f"qr/card-page      v{v}, {mods}x{mods} modules, print >= {mm}mm wide")
        print(f"                  -> {url}")

    if warnings:
        print()
        for w in warnings:
            print(f"! {w}")


if __name__ == "__main__":
    main()
