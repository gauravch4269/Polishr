"""Example community rule pack: checks that matter for small businesses in India.

To add your own pack, create another file in rules/ that exposes
RULES = [fn, ...], where fn(page) returns a list of
{"severity", "category", "finding", "fix"} dicts.
"""


def issue(severity, category, finding, fix):
    return {"severity": severity, "category": category, "finding": finding, "fix": fix}


def tel_or_whatsapp_link(page):
    for href in page.links:
        h = href.strip().lower()
        if h.startswith("tel:") or "wa.me/" in h or "api.whatsapp.com/send" in h:
            return []
    return [issue("medium", "cta_trust",
                  "No tel: or wa.me link found. Many customers in India prefer to call or WhatsApp first.",
                  'Add a tap-to-call link (<a href="tel:+91XXXXXXXXXX">) and a WhatsApp link (<a href="https://wa.me/91XXXXXXXXXX">).')]


RULES = [tel_or_whatsapp_link]
