"""Gehaltsangaben aus Anzeigen- und Mailtext erkennen und vergleichbar machen.

„48.000 € pro Jahr“, „20 € pro Stunde“, „3.500 – 4.200 € brutto/Monat“, „55k–65k EUR“, „ab 60.000 €“, „€ 25,50/Std.“
→ einheitlicher Text („48.000 €/Jahr“) plus Jahresbetrag zum Sortieren (Mitte der Spanne; Monat × 12, Stunde × 2.080).
"""
from __future__ import annotations

import re

_ZAHL = r"\d{1,3}(?:[.\s']\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?(?:\s?[kK])?"
_WAEHRUNG = r"(?:Euro|EUR|€)"
_SPANNE = rf"(?P<von>{_ZAHL})\s*(?:(?:Euro|EUR|€)\s*)?(?:[-–—]|bis)\s*(?P<bis>{_ZAHL})"
_EINE = rf"(?P<einzeln>{_ZAHL})"
_ZEIT = r"(?P<zeit>(?:pro|per|je|/|im|a)\s*(?:jahr|year|monat|month|mon\.?|stunde|std\.?|hour|h\b|a\b)|jährlich|monatlich|stündlich|p\.\s?a\.|yearly|monthly|hourly)"
_MUSTER = [
    re.compile(rf"(?:ab|from|bis zu|up to)?\s*(?:{_SPANNE}|{_EINE})\s*{_WAEHRUNG}(?:\s*(?:brutto|netto|gross))?\s*{_ZEIT}?", re.I),
    re.compile(rf"{_WAEHRUNG}\s*(?:{_SPANNE}|{_EINE})(?:\s*(?:brutto|netto|gross))?\s*{_ZEIT}?", re.I),
]
_ZEITRAUM = {"jahr": "Jahr", "year": "Jahr", "jährlich": "Jahr", "p.a.": "Jahr", "p. a.": "Jahr", "yearly": "Jahr", "a": "Jahr",
             "monat": "Monat", "month": "Monat", "mon": "Monat", "monatlich": "Monat", "monthly": "Monat",
             "stunde": "Std.", "std": "Std.", "hour": "Std.", "h": "Std.", "stündlich": "Std.", "hourly": "Std."}
_FAKTOR = {"Jahr": 1, "Monat": 12, "Std.": 2080}
_KONTEXT = re.compile(r"gehalt|vergütung|verguetung|lohn|salary|brutto|netto|verdienst|bezahlung|einkommen|jahresziel|arbeitgeberangabe|schätzung|estimate|pay\b", re.I)
_KEIN_GEHALT = re.compile(r"preis|bonus|prämie|praemie|zuschuss|budget|kosten|rabatt|umsatz|gutschein|provision|spende|miete", re.I)


def _wert(text: str) -> float | None:
    t = text.strip().replace(" ", "").replace("'", "")
    k = t[-1:] in "kK"
    t = t.rstrip("kK")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", t):
        t = t.replace(".", "")
    t = t.replace(",", ".")
    try:
        w = float(t)
    except ValueError:
        return None
    return w * 1000 if k else w


def _zeitraum(roh: str | None, betrag: float) -> str:
    if roh:
        schluessel = re.sub(r"^(?:pro|per|je|/|im|a)\s*", "", roh.strip().lower()).rstrip(".").strip()
        for k, v in _ZEITRAUM.items():
            if schluessel.startswith(k.rstrip(".")):
                return v
    # Ohne Angabe schätzen: kleine Beträge sind Stundenlöhne, mittlere Monatsgehälter
    return "Std." if betrag < 200 else ("Monat" if betrag < 15000 else "Jahr")


def _format(w: float) -> str:
    if w >= 1000 or w.is_integer():
        return f"{w:,.0f}".replace(",", ".")
    return f"{w:.2f}".replace(".", ",")


def erkenne(text: str) -> dict | None:
    """{"text": "48.000 €/Jahr", "jahr": 48000} oder None. Nimmt die erste plausible Angabe."""
    if not text or not re.search(r"€|EUR|Euro", text, re.I):
        return None
    for muster in _MUSTER:
        for m in muster.finditer(text):
            if m.group("einzeln"):
                von = bis = _wert(m.group("einzeln"))
            else:
                von, bis = _wert(m.group("von")), _wert(m.group("bis"))
                if von is not None and bis is not None and bis < von and bis * 1000 >= von:  # „55–65k“
                    von *= 1000 if von < 1000 <= bis * 1000 else 1
            if not von or not bis or von <= 0:
                continue
            umfeld = text[max(0, m.start() - 40):m.end() + 20]
            if _KEIN_GEHALT.search(umfeld) and not _KONTEXT.search(umfeld):
                continue
            if not m.group("zeit") and not _KONTEXT.search(umfeld):   # ohne Zeitraum nur mit Gehaltswort daneben
                continue
            zeit = _zeitraum(m.group("zeit"), max(von, bis))
            jahr = (von + bis) / 2 * _FAKTOR[zeit]
            if not 3000 <= jahr <= 1_000_000:          # Preise, Boni, Postleitzahlen o. ä.
                continue
            betrag = _format(von) if von == bis else f"{_format(von)}–{_format(bis)}"
            vorne = "ab " if re.match(r"\s*(?:ab|from)\b", m.group(0), re.I) else ("bis " if re.match(r"\s*(?:bis zu|up to)\b", m.group(0), re.I) else "")
            return {"text": f"{vorne}{betrag} €/{zeit}", "jahr": round(jahr)}
    return None


def ergaenze(treffer: dict) -> dict:
    """Setzt `gehalt`/`gehalt_jahr` am Treffer, falls Titel, Beschreibung oder Zusatzzeilen eine Angabe enthalten."""
    if treffer.get("gehalt"):
        return treffer
    for feld in ("gehalt_roh", "beschreibung", "titel", "beruf"):
        g = erkenne(str(treffer.get(feld) or ""))
        if g:
            treffer["gehalt"], treffer["gehalt_jahr"] = g["text"], g["jahr"]
            break
    return treffer
