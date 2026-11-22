"""Job-Alerts im Aufbau der echten Mails (nachgebaut, ohne echte Daten): jede Zeile einer Karte als eigener Link,
Outlook-Kommentare, Indeeds verpackte Links, Glassdoor, JobLeads, XING-Kurzlinks und StepStone-Empfehlungen."""

import base64
import gzip
from email.message import EmailMessage

from jobsuche import postfach


def _mail(absender: str, html: str) -> bytes:
    nachricht = EmailMessage()
    nachricht["From"] = absender
    nachricht["To"] = "person@example.org"
    nachricht["Subject"] = "Neue Jobs"
    nachricht["Date"] = "Sun, 22 Nov 2026 10:00:00 +0000"
    nachricht.set_content("Nur HTML")
    nachricht.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
    return nachricht.as_bytes()


def _indeed(ziel: str) -> str:
    token = base64.urlsafe_b64encode(gzip.compress(f'{{"u":"{ziel}"}}'.encode())).decode().rstrip("=")
    return f"https://cts.indeed.com/v3/{token}/x"


def _kurz(stellen):
    return [(s["titel"], s["firma"], s["ort"], s["referenz"]) for s in stellen]


def test_linkedin_jede_zeile_ein_eigener_link_und_outlook_kommentare():
    karte = lambda n, titel, firma: "".join(
        f"<a href='https://www.linkedin.com/comm/jobs/view/{n}/?trackingId=x'>{z}</a>"
        for z in (titel, f"{firma} · Bremen (Hybrid)", "Aktiv auf Personalsuche", "Einfach bewerben"))
    html = ("<!--[if mso]><table><tr><td><![endif]-->" + karte(4400000001, "Admin (m/w/d)", "Muster GmbH")
            + "<!--[if mso]></td></tr></table><![endif]-->" + karte(4400000002, "Entwickler", "Beispiel AG")
            + "<a href='https://www.linkedin.com/x'>Alle Jobs anzeigen</a>")
    assert _kurz(postfach.stellen_aus_mail(_mail("LinkedIn <jobalerts-noreply@linkedin.com>", html))) == [
        ("Admin (m/w/d)", "Muster GmbH", "Bremen (Hybrid)", "linkedin:4400000001"),
        ("Entwickler", "Beispiel AG", "Bremen (Hybrid)", "linkedin:4400000002")]


def test_indeed_verpackte_links_werden_ausgepackt_profil_links_sind_keine_stellen():
    stelle = _indeed("https://de.indeed.com/pagead/clk?mo=r&ad=-6NYabc123&jrtk=5")
    html = (f"<a href='{stelle}'>Techniker (m/w/d)</a><p>Muster GmbH · 22297 Hamburg</p><p>48.000 € pro Jahr</p>"
            f"<a href='{stelle}'>Job anzeigen</a>"
            f"<a href='{_indeed('https://match.indeed.com/invitations/not-interested?tk=1')}'>Passt nicht</a><p>Stellenbeschreibung</p>"
            f"<a href='{_indeed('https://profile.indeed.com/preferences?open=commute')}'>Pendelzeit</a><p>Keine Angabe</p>"
            f"<a href='{_indeed('https://profile.indeed.com/build/address')}'>Arbeitsort</a><p>12345 Irgendwo</p>")
    assert _kurz(postfach.stellen_aus_mail(_mail("Indeed <donotreply@match.indeed.com>", html))) == [
        ("Techniker (m/w/d)", "Muster GmbH", "22297 Hamburg", "indeed:ad:-6NYabc123")]


def test_glassdoor_firma_vor_dem_titel_mit_bewertung():
    link = lambda n: f"https://www.glassdoor.de/partner/jobListing.htm?pos=101&jobListingId={n}&utm_source=jobalert"
    html = (f"<a href='{link(1010)}'><span>Muster GmbH</span><span>4.0 ★</span><b>Softwareentwickler (m/w/d)</b>"
            f"<span>Bremen</span><span>Schnell bewerben</span><span>2T</span></a>"
            f"<a href='{link(1011)}'><span>Beispiel AG</span><b>IT-Administrator</b><span>Home office</span></a>"
            "<a href='https://www.glassdoor.de/Job/x.htm'>Weitere Jobs anzeigen</a>")
    stellen = postfach.stellen_aus_mail(_mail("Glassdoor Jobs <noreply@glassdoor.com>", html))
    assert _kurz(stellen) == [("Softwareentwickler (m/w/d)", "Muster GmbH", "Bremen", "glassdoor:1010"),
                              ("IT-Administrator", "Beispiel AG", "Home office", "glassdoor:1011")]
    assert stellen[0]["url"] == "https://www.glassdoor.de/partner/jobListing.htm?jobListingId=1010"
    assert stellen[0]["quelle"] == "Glassdoor (Alert)"


def test_jobleads_titel_firma_ort_stehen_vor_dem_link():
    link = lambda h: f"https://www.jobleads.com/de/jobs?job_rank=1&kuuid=k&promotedJobs=external-{h}&saved_search_email_id=x"
    html = ("<p>Neue Jobs für 3. Juli</p><p>Basierend auf deiner gespeicherten Suche</p>"
            "<p>Software Engineer - React</p><p>Muster Inc</p><p>•</p><p>Deutschland | Remote</p><p>•</p>"
            f"<p>EUR 60.000 - 80.000 | Vollzeit</p><a href='{link('71cb188f96e3e50505db7296906527b5')}'>Job ansehen</a>"
            "<p>Dein Lebenslauf-Check ist fertig!</p><a href='https://www.jobleads.com/user/kuuid'>Sieh dir deine Ergebnisse an</a>"
            "<p>Backend Lead</p><p>Beispiel LLC</p><p>•</p><p>Berlin</p>"
            f"<a href='{link('518851451cab939a72069152ab7e669a')}'>Job ansehen</a>")
    assert _kurz(postfach.stellen_aus_mail(_mail("JobLeads <jobs@mail.jobleads.com>", html))) == [
        ("Software Engineer - React", "Muster Inc", "Remote", "jobleads:71cb188f96e3e50505db7296906527b5"),
        ("Backend Lead", "Beispiel LLC", "Berlin", "jobleads:518851451cab939a72069152ab7e669a")]


def test_xing_jobmails_ja_geburtstagsmails_nein():
    html = ("<a href='https://www.xing.com/m/abcdefghijklmnA'>Im Browser ansehen</a><p>Hallo Peter,</p>"
            "<a href='https://www.xing.com/m/abcdefghijklmnB'>IT-Systemadministrator (m/w/d)</a><p>Muster KG</p><p>Bremen</p>"
            "<p>Vollzeit</p><p>42.000 € - 60.000 €</p>"
            "<a href='https://www.xing.com/m/abcdefghijklmnC'>Alle Vorschläge ansehen</a><p>Bist Du gerade auf Job-Suche?</p>"
            "<a href='https://www.xing.com/m/abcdefghijklmnD'>Ich suche gerade nichts</a><p>Hol Dir die XING App</p>")
    assert [(t, f, o) for t, f, o, _ in _kurz(postfach.stellen_aus_mail(_mail("XING Jobs <jobs@mail.xing.com>", html)))] == [
        ("IT-Systemadministrator (m/w/d)", "Muster KG", "Bremen")]
    geburtstag = "<a href='https://www.xing.com/m/abcdefghijklmnE'>Erika Muster</a><p>Beispiel GmbH</p><p>21.11.1985 - wird 41</p>"
    assert postfach.stellen_aus_mail(_mail("XING <mailrobot@mail.xing.com>", geburtstag)) == []


def test_stepstone_empfehlung_und_knoepfe_sind_keine_stellen():
    klick = lambda n: f"https://click.stepstone.de/f/a/{n}~~/AAAmIhA~/x"
    html = ("<p>Recommended for you</p><p>IT-Admin...</p><p>Muster GmbH</p><p>Essen</p>"
            f"<a href='{klick('A1')}'>I'm interested</a>"
            f"<a href='{klick('B2')}'><span>Because you viewed</span><b>Admin Microsoft 365</b><span>in</span><span>Nortmoor</span></a>"
            f"<a href='{klick('C3')}'>IT-Systemadministrator (m/w/d)</a><p>Beispiel Datenservice GmbH</p><p>Düsseldorf</p>"
            f"<a href='{klick('D4')}'>Report this job</a><p>Boost your chances</p>"
            f"<a href='{klick('E5')}'>Unsubscribe from this email</a><p>Please be aware</p><p>© 2026 Stepstone</p>")
    assert [(t, f, o) for t, f, o, _ in _kurz(postfach.stellen_aus_mail(_mail("Stepstone <info@jobagent.stepstone.de>", html)))] == [
        ("IT-Systemadministrator (m/w/d)", "Beispiel Datenservice GmbH", "Düsseldorf")]
