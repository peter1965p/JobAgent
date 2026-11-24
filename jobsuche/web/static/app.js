"use strict";
// Job Agent Web. Alles Fremde (Firmentexte, Treffer, Mails) wird als Text gesetzt, nie als HTML.

const Z = { csrf: "", ich: null, seite: "start", auftraege: new Set() };

// --- DOM-Helfer --------------------------------------------------------------------------------------------------------
function el(tag, attrs, ...kinder) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kind of kinder.flat(Infinity)) {
    if (kind === null || kind === undefined || kind === false) continue;
    e.append(kind instanceof Node ? kind : document.createTextNode(String(kind)));
  }
  return e;
}
const $ = (id) => document.getElementById(id);
// replaceChildren macht aus null den Text „null“: vorher ausfiltern
function rc(ziel, ...kinder) { ziel.replaceChildren(...kinder.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false)); }
function karte(titel, ...inhalt) { return el("div", { class: "karte" }, titel ? el("h2", {}, titel) : null, ...inhalt); }
function knopf(text, aktion, art) { return el("button", { type: "button", class: art || "", onclick: aktion }, text); }

function toast(text, art) {
  const t = $("toast");
  t.textContent = text;
  t.className = "toast " + (art || "");
  t.hidden = false;
  clearTimeout(toast.uhr);
  toast.uhr = setTimeout(() => { t.hidden = true; }, art === "fehler" ? 9000 : 5000);
}

function dialog(titel, inhalt, knoepfe) {
  const d = $("dialoginhalt");
  rc(d, el("h2", {}, titel), ...[].concat(inhalt), el("div", { class: "reihe" }, ...(knoepfe || [knopf("Schließen", schliesseDialog)])));
  $("dialog").hidden = false;
}
function schliesseDialog() { $("dialog").hidden = true; }

// --- API ---------------------------------------------------------------------------------------------------------------
async function api(pfad, methode, daten, roh) {
  const opt = { method: methode || "GET", headers: {}, credentials: "same-origin" };
  if (opt.method !== "GET") {
    opt.headers["X-Jobagent"] = "1";
    opt.headers["X-CSRF"] = Z.csrf;
  }
  if (daten instanceof FormData) opt.body = daten;
  else if (daten !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(daten); }
  const r = await fetch("api/" + pfad, opt);
  if (r.status === 401 && pfad !== "anmelden") { zeigeAnmeldung(); throw new Error("Bitte anmelden."); }
  if (roh) return r;
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (!r.ok) throw new Error((j && j.detail) || `Fehler ${r.status}`);
  return j;
}

async function versuche(f) {
  try { return await f(); } catch (e) { toast(e.message, "fehler"); return undefined; }
}

// Startet einen Hintergrundauftrag und wartet auf ihn. Fortschritt steht in der Pille oben rechts.
async function auftrag(pfad, daten, nachher) {
  const a = await versuche(() => api(pfad, "POST", daten));
  if (!a) return;
  Z.auftraege.add(a.auftrag);
  beschaeftigt(a.titel + " …");
  for (;;) {
    await new Promise((ok) => setTimeout(ok, 1500));
    let s;
    try { s = await api("auftraege/" + a.auftrag); } catch (e) { toast(e.message, "fehler"); break; }
    if (s.status === "laeuft" || s.status === "wartet") { beschaeftigt(`${s.titel}: ${s.fortschritt || "läuft"}`); continue; }
    Z.auftraege.delete(a.auftrag);
    if (s.status === "fehler") toast(`${s.titel}: ${s.fehler}`, "fehler");
    else if (nachher) await nachher(s.ergebnis);
    else toast(`${s.titel}: fertig`, "erfolg");
    break;
  }
  if (!Z.auftraege.size) await ladeIch();
}

function beschaeftigt(text) {
  $("pill").textContent = text;
  $("pill").className = "pill beschaeftigt";
  $("ring").classList.add("dreht");
}

// --- Anmeldung ---------------------------------------------------------------------------------------------------------
let registrieren = false;
function zeigeAnmeldung() {
  $("app").hidden = true;
  $("anmeldung").hidden = false;
  $("a_email").focus();
}
$("a_wechsel").addEventListener("click", () => {
  registrieren = !registrieren;
  $("a_name").hidden = $("a_code").hidden = !registrieren;
  $("a_code").required = registrieren;
  $("a_los").textContent = registrieren ? "Konto anlegen" : "Anmelden";
  $("a_wechsel").textContent = registrieren ? "Zur Anmeldung" : "Mit Einladung registrieren";
  $("a_passwort").autocomplete = registrieren ? "new-password" : "current-password";
  $("anmeldetext").textContent = registrieren
    ? "Konto mit Einladungscode anlegen. Passwort: mindestens 12 Zeichen."
    : "Melde dich mit deinem Konto an. Neue Konten gibt es nur mit Einladung.";
});
$("anmeldeformular").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const m = $("a_meldung");
  m.hidden = true;
  const daten = { email: $("a_email").value, passwort: $("a_passwort").value };
  if (registrieren) Object.assign(daten, { code: $("a_code").value, name: $("a_name").value });
  try {
    const r = await api(registrieren ? "registrieren" : "anmelden", "POST", daten);
    Z.csrf = r.csrf;
    $("a_passwort").value = "";
    $("a_code").value = "";
    await starteApp();
  } catch (e) {
    m.textContent = e.message;
    m.hidden = false;
    $("a_passwort").value = "";
  }
});

// --- Rahmen ------------------------------------------------------------------------------------------------------------
const NAVIGATION = [
  ["ARBEIT", [["start", "Start"], ["bericht", "Morgenbericht"], ["suche", "Suche"], ["firmen", "Firmen"], ["entwuerfe", "Entwürfe"], ["gesendet", "Gesendet"]]],
  ["PROFIL", [["profil", "Profil"], ["fakten", "Fakten"], ["mailkonto", "Mailkonto"]]],
  ["KI", [["system", "System"]]],
];
const TITEL = {
  start: ["Start", "Stand der Jobsuche und nächster Schritt"],
  bericht: ["Morgenbericht", "Was der Nachtlauf gefunden hat. Gesendet wird nichts, die Mails warten auf deine Freigabe"],
  suche: ["Suche", "Findet Firmen in Verzeichnis, Internet, Jobbörse, Job-Alerts und Portalen"],
  firmen: ["Firmen", "Recherche, Bewertung und Entwurf pro Firma"],
  entwuerfe: ["Entwürfe", "Prüfen, ändern, freigeben, senden"],
  gesendet: ["Gesendet", "Gesendete Mails und ihr Ergebnis"],
  profil: ["Profil", "Name, Absender, Stil und Unterlagen"],
  fakten: ["Fakten", "Was über dich in Mails darf – nur Bestätigtes"],
  mailkonto: ["Mailkonto", "Dein eigenes Konto für den Versand"],
  system: ["System", "KI, Daten und Sicherung"],
  admin: ["Verwaltung", "Einladungen, Mandanten, Konten"],
  konto: ["Konto", "Passwort, KI und Mandant"],
};

function baueNavigation() {
  const nav = $("navigation");
  rc(nav, );
  const gruppen = NAVIGATION.slice();
  if (Z.ich && Z.ich.konto.admin) gruppen.push(["BETRIEB", [["admin", "Verwaltung"]]]);
  for (const [sektion, eintraege] of gruppen) {
    nav.append(el("div", { class: "navsektion" }, sektion));
    for (const [id, text] of eintraege) {
      nav.append(el("button", { type: "button", class: "naveintrag" + (Z.seite === id ? " aktiv" : ""), "data-seite": id,
        onclick: () => zeige(id) }, text));
    }
  }
}

async function ladeIch() {
  const ich = await api("ich");
  Z.ich = ich;
  Z.csrf = ich.csrf;
  const name = ich.person || ich.konto.name || ich.konto.email;
  const teile = name.trim().split(/\s+/);
  const initialen = teile[0] ? (teile[0][0] + (teile.length > 1 ? teile[teile.length - 1][0] : "")).toUpperCase() : "?";
  zeigeAvatar(ich.konto.avatar, initialen);
  const m = ich.modell_wirksam;
  const ki = m.startsWith("claude:") ? m.slice(7) + " · Claude" : m.startsWith("cf:") ? m.split("/").pop() + " · Cloudflare" : m + " · lokal";
  const mandant = ich.mandant.name && ich.mandant.name !== name ? `${ich.mandant.name}${ich.mandanten.length > 1 ? " ▾" : ""}\n` : "";
  $("fusstext").textContent = `${name}\n${mandant}Job Agent ${ich.version}\nKI: ${ki}`;
  if (!Z.auftraege.size) {
    $("pill").textContent = `${ich.stand.firmen} Firmen · ${ich.stand.fakten} Fakten · ${ich.stand.antworten} Antworten`;
    $("pill").className = "pill";
    $("ring").classList.remove("dreht");
  }
  baueNavigation();
}

$("profilknopf").addEventListener("click", (ev) => {
  ev.stopPropagation();
  const m = $("kontomenue");
  if (!m.hidden) { m.hidden = true; return; }
  const ich = Z.ich;
  rc(m, 
    el("div", { class: "menutitel" }, ich.konto.email),
    knopf("Profil", () => { m.hidden = true; zeige("profil"); }),
    knopf("Konto-Einstellungen", () => { m.hidden = true; zeige("konto"); }),
    knopf("Mailkonto", () => { m.hidden = true; zeige("mailkonto"); }),
    knopf(ich.konto.avatar ? "Avatar ändern" : "Avatar hochladen", () => { m.hidden = true; $("avatardatei").click(); }),
    ich.konto.avatar ? knopf("Avatar entfernen", async () => {
      m.hidden = true;
      if (await versuche(() => api("konto/avatar", "DELETE"))) await ladeIch();
    }) : null,
    ich.mandanten.length > 1 ? el("div", { class: "menutitel" }, "MANDANT WECHSELN") : null,
    ich.mandanten.length > 1 ? ich.mandanten.map((x) => el("button", { type: "button", class: x.id === ich.mandant.id ? "gewaehlt" : "",
      onclick: async () => { m.hidden = true; if (await versuche(() => api("mandant", "POST", { id: x.id }))) { await ladeIch(); zeige("start"); } } },
      (x.id === ich.mandant.id ? "● " : "○ ") + x.name)) : null,
    knopf("Abmelden", abmelden),
  );
  m.hidden = false;
});
document.addEventListener("click", () => { $("kontomenue").hidden = true; });
$("menuknopf").addEventListener("click", () => $("seitenleiste").classList.toggle("offen"));

function zeigeAvatar(hat, initialen) {
  const a = $("avatar");
  a.textContent = "";
  a.classList.toggle("bild", !!hat);
  if (hat) a.append(el("img", { src: `api/konto/avatar?v=${Date.now()}`, alt: initialen }));
  else a.textContent = initialen;
}

$("avatardatei").addEventListener("change", async (ev) => {
  const datei = ev.target.files[0];
  ev.target.value = "";
  if (!datei) return;
  if (datei.size > 512 * 1024) { toast("Das Bild ist zu groß (höchstens 512 KB).", "fehler"); return; }
  const fd = new FormData();
  fd.append("datei", datei);
  if (await versuche(() => api("konto/avatar", "POST", fd))) await ladeIch();
});

async function abmelden() {
  await versuche(() => api("abmelden", "POST", {}));
  Z.ich = null;
  Z.csrf = "";
  zeigeAnmeldung();
}

async function zeige(id) {
  Z.seite = id;
  $("seitenleiste").classList.remove("offen");
  const [t, u] = TITEL[id] || [id, ""];
  $("titel").textContent = t;
  $("untertitel").textContent = u;
  baueNavigation();
  const ziel = $("seite");
  const nr = (Z.nav = (Z.nav || 0) + 1);
  rc(ziel, el("div", { class: "gedimmt" }, "Lade …"));
  const neu = el("div");  // erst fertig bauen, dann einsetzen: Wer schnell weiterklickt, sieht keine veraltete Seite
  try {
    await SEITEN[id](neu);
  } catch (e) {
    rc(neu, karte("Fehler", el("div", { class: "fehler" }, e.message)));
  }
  if (nr !== Z.nav) return;
  rc(ziel, [...neu.childNodes]);
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
}

async function starteApp() {
  await ladeIch();
  $("anmeldung").hidden = true;
  $("app").hidden = false;
  const ziel = location.hash.slice(1);
  await zeige(SEITEN[ziel] ? ziel : "start");
}

// --- Seiten ------------------------------------------------------------------------------------------------------------
const SEITEN = {};

SEITEN.start = async (ziel) => {
  const s = await api("start");
  const werte = el("div", { class: "werte" }, s.werte.map(([n, w]) => [el("span", { class: "gedimmt" }, n), el("span", {}, w)]));
  rc(ziel, el("div", { class: "raster" },
    karte("Stand", werte),
    karte("Nächster Schritt", el("p", {}, s.schritt), knopf("Öffnen", () => zeige(SEITEN[s.ziel] ? s.ziel : "profil"), "primaer")),
    karte("So bleibt es kontrolliert", el("p", { class: "gedimmt" },
      "Mailentwürfe sehen nur Fakten, die du bestätigt hast, und öffentliche Firmentexte. Ohne deine Freigabe wird nichts " +
      "als sendefertig markiert, gesendet wird nur über dein eigenes Mailkonto und nur auf deinen Klick. Deine Daten liegen " +
      "in einem eigenen, abgeschotteten Bereich der Datenbank."))));
};

SEITEN.bericht = async (ziel) => {
  const b = await api("bericht");
  const uhr = el("select", {}, Array.from({ length: 24 }, (_, h) => {
    const t = String(h).padStart(2, "0") + ":00";
    return el("option", { value: t, selected: t === b.uhrzeit ? "selected" : null }, t);
  }));
  const steuerung = karte("Nachtlauf",
    el("p", { class: "gedimmt" }, "Läuft auf dem Server, auch wenn kein Rechner an ist. Er sucht mit den Angaben der Suchseite, " +
      "liest Firmenseiten, bewertet und schreibt bis zu drei Mailvorschläge. Gesendet wird nichts."),
    el("div", { class: "reihe" }, uhr,
      knopf(b.nachtlauf ? "Nachtlauf ausschalten" : "Nachtlauf einschalten", async () => {
        if (await versuche(() => api("bericht/nachtlauf", "POST", { an: !b.nachtlauf, uhrzeit: uhr.value }))) zeige("bericht");
      }, "primaer"),
      knopf("Jetzt laufen lassen", () => auftrag("bericht/jetzt", {}, () => { toast("Nachtlauf fertig", "erfolg"); zeige("bericht"); })),
      b.nachtlauf ? el("span", { class: "marke gruen" }, "eingeschaltet " + b.uhrzeit) : el("span", { class: "marke" }, "aus")));
  const r = b.bericht;
  if (!r) { rc(ziel, steuerung, karte("Aussichtsreichste Firmen", el("p", { class: "gedimmt" }, "Noch kein Bericht."))); return; }
  const grund = el("div", { class: "details gedimmt" });
  const tab = tabelle(["Score", "Firma", "Inhaber", "Adresse", "Art", "Weg", "Stellen", "Mail"],
    r.firmen.map((f) => ({ zellen: [f.score, f.name, f.personen, f.email, f.art, f.direkt ? "direkt" : "Börse", (f.stellen || []).length || "", f.entwurf_id ? "fertig" : ""],
      klick: () => { grund.textContent = ((f.stellen || []).length ? "Auf der eigenen Website: " + f.stellen.join("; ") + "\n" : "") + (f.begruendung || ""); } })));
  rc(ziel, steuerung, karte("Aussichtsreichste Firmen",
    el("p", { class: "gedimmt" }, `Lauf vom ${r.datum.slice(0, 16).replace("T", " ")}: ${r.gefunden} gefunden, ${r.direkt || 0} davon nicht aus Stellenbörsen, ` +
      `${r.geprueft} Firmenseiten gelesen, ${r.ohne_adresse} ohne veröffentlichte Adresse und ${r.zu_niedrig} mit zu niedrigem Score aussortiert.`),
    r.fehler ? el("p", { class: "fehler" }, "Fehler: " + r.fehler) : null,
    (r.hinweise || []).length ? el("p", { class: "hinweis details" }, r.hinweise.join("\n")) : null,
    tab, grund, knopf("Zu den Mailvorschlägen", () => zeige("entwuerfe"))));
};

// Tabelle: zeilen = [{zellen: [...], klick, id}], spalten optional sortierbar (sortiere(index))
function tabelle(kopf, zeilen, sortiere, sortierung) {
  const thead = el("tr", {}, kopf.map((k, i) => el("th", { class: sortiere ? "sortierbar" : "", onclick: sortiere ? () => sortiere(i) : null },
    k + (sortierung && sortierung[0] === i ? (sortierung[1] ? " ▼" : " ▲") : ""))));
  const body = el("tbody", {}, zeilen.map((z) => {
    const tr = el("tr", { class: z.klick ? "klickbar" : "" }, z.zellen.map((c) => el("td", {}, c instanceof Node ? c : (c ?? ""))));
    if (z.klick) tr.addEventListener("click", () => { body.querySelectorAll("tr").forEach((x) => x.classList.remove("gewaehlt")); tr.classList.add("gewaehlt"); z.klick(); });
    return tr;
  }));
  return el("div", { class: "tabellenrahmen" }, el("table", {}, el("thead", {}, thead), body));
}

const SPALTEN = [["naehe", "Nähe"], ["firma", "Firma"], ["titel", "Worum es geht"], ["ort", "Ort"], ["km", "km"], ["quelle", "Quelle"],
  ["status", "Status"], ["ki", "KI"], ["alter", "Alter"]];
const QUELLEN = [["verzeichnis", "Firmenverzeichnis"], ["internet", "Internet"], ["jobboerse", "Jobbörse"], ["arbeitnow", "Arbeitnow"],
  ["postfach", "Job-Alerts"], ["portale", "Portale"]];
const S = { seite: 0, pro: 50, sort: "", ab: false, gewaehlt: null };

SEITEN.suche = async (ziel) => {
  const d = await api("suche");
  const w = d.werte;
  const f = {
    begriffe: el("input", { value: w.begriffe, placeholder: "Berufe: werden aus deinen Fakten abgeleitet", maxlength: 500 }),
    land: el("select", {}, Object.entries(d.laender).map(([c, n]) => el("option", { value: c, selected: c === w.land ? "selected" : null }, n))),
    bundesland: el("select", {}, el("option", { value: "" }, "Umkreis um den Ort"),
      d.bundeslaender.map((b) => el("option", { value: b, selected: b === w.bundesland ? "selected" : null }, "Ganz " + b))),
    ort: el("input", { value: w.ort, placeholder: "Ort oder Region, leer: ganzes Land", maxlength: 120 }),
    umkreis: el("select", {}, [10, 25, 50, 100, 200, 0].map((k) => el("option", { value: k, selected: k === w.umkreis ? "selected" : null }, k ? k + " km" : "Ganze Region"))),
    branche: el("select", {}, el("option", { value: "" }, "Branche aus Fakten"),
      Object.entries(d.branchen).map(([c, n]) => el("option", { value: c, selected: c === w.branche ? "selected" : null }, n))),
    arbeitszeit: el("select", {}, Object.entries(d.arbeitszeit).map(([c, n]) => el("option", { value: c, selected: c === w.arbeitszeit ? "selected" : null }, n))),
    remote: el("select", {}, Object.entries(d.remote).map(([c, n]) => el("option", { value: c, selected: c === w.remote ? "selected" : null }, n))),
    max_tage: el("input", { type: "number", min: 0, max: 100, value: w.max_tage, title: "höchstens … Tage alt, 0: jedes Alter" }),
    max_treffer: el("input", { type: "number", min: 0, max: 2000, step: 50, value: w.max_treffer, title: "max. Treffer, 0: ohne Grenze" }),
    aufgefrischte: el("input", { type: "checkbox", checked: w.aufgefrischte }),
    nur_ki: el("input", { type: "checkbox", checked: w.nur_ki }),
    stichwoerter: el("input", { value: w.stichwoerter, placeholder: w.abgeleitet_stichwoerter ? "Stichwörter, leer: " + w.abgeleitet_stichwoerter : "Stichwörter für Titel", maxlength: 500 }),
    ausschluss: el("input", { value: w.ausschluss, placeholder: "Ausschließen: Wörter mit Komma, z. B. TYPO3, Zeitarbeit", maxlength: 500 }),
  };
  const quellen = Object.fromEntries(QUELLEN.map(([q]) => [q, el("input", { type: "checkbox", checked: w.quellen.includes(q) })]));
  const sammeln = (modus) => ({
    modus, begriffe: f.begriffe.value, land: f.land.value, bundesland: f.bundesland.value, ort: f.ort.value,
    umkreis: Number(f.umkreis.value), branche: f.branche.value, arbeitszeit: f.arbeitszeit.value, remote: f.remote.value,
    max_tage: Number(f.max_tage.value || 0), max_treffer: Number(f.max_treffer.value || 0), aufgefrischte: f.aufgefrischte.checked,
    nur_ki: f.nur_ki.checked, stichwoerter: f.stichwoerter.value, ausschluss: f.ausschluss.value,
    quellen: QUELLEN.map(([q]) => q).filter((q) => quellen[q].checked),
  });
  const hinweise = el("div", { class: "hinweis details klein" });
  const liste = el("div");
  const details = el("div", { class: "spalte" }, el("p", { class: "gedimmt" }, "Klicke auf einen Treffer."));
  const nachSuche = async (e) => {
    hinweise.textContent = [...(e.hinweise || []), e.zeiten_text || ""].filter(Boolean).join("\n");
    if (e.begriffe) f.begriffe.value = e.begriffe.join(", ");
    S.seite = 0; S.sort = ""; S.gewaehlt = null;
    toast(`${e.anzahl} Treffer`, e.anzahl ? "erfolg" : "");
    await zeigeTreffer();
  };
  const zeigeTreffer = async () => {
    const r = await api(`suche/treffer?seite=${S.seite}&pro_seite=${S.pro}&sortierung=${S.sort}&ab=${S.ab}`);
    S.seite = r.seite;
    const sortIdx = SPALTEN.findIndex(([k]) => k === S.sort);
    const tab = tabelle(SPALTEN.map(([, t]) => t), r.zeilen.map((z) => ({
      zellen: [z.naehe ?? "", z.firma, z.titel, (z.remote ? "🏠 " : "") + z.ort, z.km, z.quelle, z.status, z.ki,
        el("span", { title: z.alter_text }, z.ampel ? el("span", { class: "punkt " + z.ampel }) : null, z.alter ?? "")],
      klick: () => zeigeDetail(z.i),
    })), (i) => { const k = SPALTEN[i][0]; S.ab = S.sort === k ? !S.ab : k === "naehe"; S.sort = k; S.seite = 0; zeigeTreffer(); },
    sortIdx >= 0 ? [sortIdx, S.ab] : null);
    const von = r.gesamt ? r.seite * r.pro_seite + 1 : 0;
    const bis = Math.min(r.gesamt, (r.seite + 1) * r.pro_seite);
    const pro = el("select", { onchange: (ev) => { S.pro = Number(ev.target.value); S.seite = 0; zeigeTreffer(); } },
      [25, 50, 100].map((n) => el("option", { value: n, selected: n === S.pro ? "selected" : null }, n + " pro Seite")));
    const nummern = [...new Set([0, r.seiten - 1, ...Array.from({ length: 5 }, (_, i) => r.seite - 2 + i)])].filter((i) => i >= 0 && i < r.seiten).sort((a, b) => a - b);
    const leiste = el("div", { class: "seitenleiste-unten" },
      el("span", { class: "gedimmt" }, r.gesamt ? `Treffer ${von}–${bis} von ${r.gesamt}` : "Keine Treffer"), el("span", { class: "spacer" }),
      pro, knopf("‹", () => { S.seite--; zeigeTreffer(); }), r.seiten > 1 ? nummern.flatMap((i, k) => [
        k && i - nummern[k - 1] > 1 ? el("span", { class: "gedimmt" }, "…") : null,
        el("button", { type: "button", class: i === r.seite ? "aktuell" : "", onclick: () => { S.seite = i; zeigeTreffer(); } }, i + 1)]) : null,
      knopf("›", () => { S.seite++; zeigeTreffer(); }));
    leiste.querySelectorAll("button")[0].disabled = r.seite <= 0;
    leiste.querySelectorAll("button")[leiste.querySelectorAll("button").length - 1].disabled = r.seite >= r.seiten - 1;
    rc(liste, tab, leiste);
  };
  const zeigeDetail = async (i) => {
    S.gewaehlt = i;
    const t = await versuche(() => api("suche/treffer/" + i));
    if (!t) return;
    const v = t.vorschau;
    const teile = [el("b", {}, t.firma), el("div", {}, t.titel), el("div", { class: "gedimmt" }, [t.ort, t.km ? t.km + " km" : ""].filter(Boolean).join(" · ")),
      el("div", { class: "gedimmt" }, "Quelle: " + t.quelle), t.alter_text ? el("div", {}, t.alter_text) : null,
      t.remote ? el("div", {}, "🏠 " + t.remote) : null, t.ki ? el("div", {}, "KI in der Anzeige: " + t.ki) : null,
      el("div", { class: "reihe" }, t.links.map((l) => el("a", { href: l.url, target: "_blank", rel: "noopener noreferrer" }, l.text)))];
    if (v) {
      if (v.problem) teile.push(el("div", { class: "hinweis" }, v.problem));
      if (v.kurztext) teile.push(el("div", { class: "gedimmt" }, v.kurztext));
      if (!v.problem) teile.push(el("div", {}, "Inhaber: " + (v.personen || "nicht gefunden")), el("div", {}, "Adresse: " + (v.email ? `${v.email} (${v.art})` : "keine veröffentlicht")));
      if ((v.stellen || []).length) teile.push(el("div", { class: "details" }, "Auf der eigenen Website:\n" + v.stellen.slice(0, 5).join("\n")));
      if (v.konzern) teile.push(el("div", { class: "hinweis" }, "Eher Konzern: " + v.konzern));
    } else {
      teile.push(knopf("Firmenseite lesen", () => auftrag(`suche/treffer/${i}/vorschau`, {}, () => zeigeDetail(i))));
    }
    teile.push(knopf("Sofort bewerben", () => auftrag(`suche/treffer/${i}/sofort`, {}, (e) => {
      const text = [e.eignung_text, e.hinweis, e.entwurf_id ? "Die Mail liegt unter „Entwürfe“. Prüfe sie und gib sie frei, erst dann wird gesendet." : ""].filter(Boolean).join("\n\n");
      dialog("Sofort bewerben", el("div", { class: "details" }, text), [knopf("Zu den Entwürfen", () => { schliesseDialog(); zeige("entwuerfe"); }, "primaer"), knopf("Schließen", schliesseDialog)]);
    }), "primaer"),
    knopf("Übernehmen und Inhaber finden", () => auftrag(`suche/treffer/${i}/inhaber`, {}, (e) => toast(inhaberText(e), "erfolg"))),
    knopf("Nur übernehmen", () => auftrag(`suche/treffer/${i}/uebernehmen`, {}, () => { toast("Übernommen", "erfolg"); zeigeTreffer(); })));
    rc(details, ...teile);
  };
  const qreihe = el("div", { class: "reihe" }, QUELLEN.map(([q, n]) => el("label", { class: "reihe" }, quellen[q], n)));
  rc(ziel, 
    karte("Wo und was", el("div", { class: "spalte" },
      f.begriffe,
      el("div", { class: "reihe" }, f.land, f.bundesland, f.ort, f.umkreis, f.branche, f.arbeitszeit, f.remote),
      el("div", { class: "reihe" }, el("label", {}, "höchstens Tage alt "), f.max_tage, el("label", {}, "max. Treffer "), f.max_treffer,
        el("label", { class: "reihe" }, f.aufgefrischte, "Aufgefrischte behalten"), el("label", { class: "reihe" }, f.nur_ki, "KI erwünscht")),
      qreihe, el("div", { class: "reihe" }, f.stichwoerter, f.ausschluss),
      el("div", { class: "reihe" },
        knopf("Passende Firmen finden", () => auftrag("suche", sammeln("passende"), nachSuche), "primaer"),
        knopf("Mit diesen Angaben suchen", () => auftrag("suche", sammeln("suchen"), nachSuche), "blau")))),
    el("div", { class: "zweispaltig" }, karte("Treffer", el("p", { class: "gedimmt klein" },
      "Die Nähe vergleicht nur Name und Kurztext mit deinen Fakten. Daten: © OpenStreetMap-Mitwirkende, Stellen auch von arbeitnow.com."),
    hinweise, liste), karte("Details", details)));
  await zeigeTreffer();
};

function inhaberText(e) {
  if (e.fehler) return `${e.name} steht in der Liste. ${e.fehler}`;
  return `${e.name}: ${e.personen || "keinen Namen gefunden"}, ${e.email ? `${e.email} (${e.art})` : "keine Adresse veröffentlicht"}. Prüfe beides auf der Website.`;
}

SEITEN.firmen = async (ziel) => {
  const liste = await api("firmen");
  const detail = el("div", { class: "spalte" }, el("p", { class: "gedimmt" }, "Wähle eine Firma."));
  const zeigeFirma = async (id) => {
    const f = await versuche(() => api("firmen/" + id));
    if (!f) return;
    const score = el("input", { type: "number", min: 0, max: 100, value: f.mein_score ?? f.ki_score ?? "" });
    const notiz = el("textarea", { maxlength: 2000 }, f.meine_notiz || "");
    rc(detail, 
      el("b", {}, f.name), el("div", { class: "gedimmt" }, f.sitz), el("div", {}, "An: " + (f.personen || "–")), el("div", {}, f.email || "keine Adresse"),
      f.website ? el("a", { href: f.website, target: "_blank", rel: "noopener noreferrer" }, f.website) : null,
      f.warum ? el("div", { class: "details gedimmt" }, "Recherche: " + f.warum) : null,
      el("div", { class: "gedimmt" }, f.seiten_geladen ? `Seiten geladen: ${f.seiten_geladen} (${f.abschnitte} Abschnitte)` : "Seiten noch nicht geladen."),
      f.ki_score !== null ? el("div", {}, el("span", { class: "marke orange" }, "KI-Score " + f.ki_score), " ", f.ki_begruendung || "") : null,
      f.belege.length ? el("div", { class: "gedimmt klein" }, "Belege: " + f.belege.join(", ")) : null,
      f.ungeprueft.length ? el("div", { class: "hinweis klein" }, "Ungeprüft: " + f.ungeprueft.join("; ")) : null,
      f.eignung ? el("div", { class: "details" }, f.eignung) : null,
      el("div", { class: "reihe" },
        knopf("Inhaber finden", () => auftrag(`firmen/${id}/inhaber`, {}, (e) => { toast(inhaberText(e), "erfolg"); zeigeFirma(id); })),
        knopf("Bewerten", () => auftrag(`firmen/${id}/bewerten`, {}, () => { toast("Bewertet", "erfolg"); zeigeFirma(id); })),
        knopf("Eignung prüfen", () => auftrag(`firmen/${id}/eignung`, {}, (e) => { dialog("Eignung", el("div", { class: "details" }, e.text)); zeigeFirma(id); }))),
      knopf("Mail vorschlagen", () => auftrag(`firmen/${id}/mail`, {}, () => { toast("Entwurf erstellt", "erfolg"); zeige("entwuerfe"); }), "primaer"),
      el("h2", {}, "Eigene Bewertung"), score, notiz,
      el("div", { class: "reihe" }, knopf("Bewertung speichern", async () => {
        if (await versuche(() => api(`firmen/${id}/bewertung`, "POST", { score: Number(score.value), notiz: notiz.value }))) toast("Gespeichert, die KI lernt daraus", "erfolg");
      }), knopf("Entfernen", async () => {
        if (!confirm(`${f.name} entfernen?`)) return;
        if (await versuche(() => api("firmen/" + id, "DELETE"))) zeige("firmen");
      }, "gefahr")));
  };
  const neu = () => {
    const felder = { name: el("input", { placeholder: "Firmenname", maxlength: 200 }), website: el("input", { placeholder: "https://…", maxlength: 300 }),
      sitz: el("input", { placeholder: "Sitz", maxlength: 200 }), personen: el("input", { placeholder: "Inhaber/Ansprechperson", maxlength: 200 }),
      email: el("input", { placeholder: "Mailadresse", maxlength: 254 }), warum: el("textarea", { placeholder: "Warum diese Firma?", maxlength: 2000 }) };
    dialog("Firma eintragen", Object.values(felder), [knopf("Speichern", async () => {
      const daten = Object.fromEntries(Object.entries(felder).map(([k, v]) => [k, v.value]));
      if (await versuche(() => api("firmen", "POST", daten))) { schliesseDialog(); zeige("firmen"); }
    }, "primaer"), knopf("Abbrechen", schliesseDialog)]);
  };
  rc(ziel, el("div", { class: "zweispaltig" },
    karte("Firmen", el("div", { class: "reihe" }, knopf("Firma eintragen", neu)),
      tabelle(["Firma", "Sitz", "Passung", "Score", "Mail"], liste.map((f) => ({ zellen: [f.name, f.sitz, f.passung, f.score ?? "", f.mail], klick: () => zeigeFirma(f.id) })))),
    karte("Details", detail)));
};

async function entwurfsSeite(ziel, gesendet) {
  const liste = await api("entwuerfe" + (gesendet ? "?gesendet=true" : ""));
  const rechts = el("div", { class: "spalte" });
  const eintraege = el("div", { class: "liste" });
  const lade = async (id) => {
    eintraege.querySelectorAll(".listeneintrag").forEach((b) => b.classList.toggle("aktiv", b.dataset.id === String(id)));
    const e = await versuche(() => api("entwuerfe/" + id));
    if (!e) return;
    const offen = e.status === "entwurf" || e.status === "freigegeben";
    const an = el("input", { value: e.empfaenger, readonly: !offen, maxlength: 254 });
    const betreff = el("input", { value: e.betreff, readonly: !offen, maxlength: 300 });
    const text = el("textarea", { readonly: !offen, maxlength: 20000, rows: 18 }, e.text);
    const befunde = e.befunde.length ? e.befunde.map((b) => el("div", { class: "befund " + b.stufe }, b.text))
      : [el("div", { class: "befund gut" }, "Prüfung ohne Befund.")];
    rc(rechts, 
      el("div", { class: "gedimmt" }, `${e.firma} · Von: ${e.von} · ${e.status_text} · Modell: ${e.modell}`),
      el("label", {}, "An"), an, el("label", {}, "Betreff"), betreff, text,
      el("div", { class: "gedimmt klein" }, "Anhänge: " + (e.anhaenge.map((a) => a.name + (a.da ? "" : " (fehlt)")).join(", ") || "keine")),
      ...befunde,
      offen ? el("div", { class: "reihe" },
        knopf("Speichern", async () => { if (await versuche(() => api("entwuerfe/" + id, "PUT", { empfaenger: an.value, betreff: betreff.value, text: text.value }))) { toast("Gespeichert, die KI lernt aus der Änderung", "erfolg"); lade(id); } }),
        knopf("Freigeben", async () => { if (await versuche(() => api(`entwuerfe/${id}/freigeben`, "POST", {}))) { toast("Freigegeben", "erfolg"); entwurfsSeite(ziel, gesendet); } }, "primaer"),
        el("button", { type: "button", class: "primaer", disabled: e.status !== "freigegeben", onclick: () => {
          dialog("Jetzt senden?", el("p", {}, `An ${e.empfaenger}, Betreff „${e.betreff}“. Anhänge: ${e.anhaenge.map((a) => a.name).join(", ") || "keine"}. Eine Kopie geht per Bcc an dich.`),
            [knopf("Senden", () => { schliesseDialog(); auftrag(`entwuerfe/${id}/senden`, {}, () => { toast("Gesendet", "erfolg"); entwurfsSeite(ziel, gesendet); }); }, "primaer"), knopf("Abbrechen", schliesseDialog)]);
        } }, "Jetzt senden"),
        knopf("Varianten …", () => auftrag(`entwuerfe/${id}/varianten`, {}, (vs) => {
          dialog("Welche Fassung?", vs.map((v, k) => el("div", { class: "karte spalte" }, el("div", { class: "details" }, v.text),
            knopf("Diese übernehmen", () => { text.value = v.text; schliesseDialog(); toast("In den Editor übernommen – „Speichern“ nicht vergessen"); }))));
        })),
        knopf("Verwerfen", async () => { if (confirm("Entwurf verwerfen?") && await versuche(() => api(`entwuerfe/${id}/verwerfen`, "POST", {}))) entwurfsSeite(ziel, gesendet); }, "gefahr"))
        : el("div", { class: "reihe" }, ["antwort", "absage", "keine_antwort"].map((s) => knopf({ antwort: "Antwort erhalten", absage: "Absage", keine_antwort: "Keine Antwort" }[s],
          async () => { if (await versuche(() => api(`entwuerfe/${id}/status`, "POST", { status: s }))) { toast("Ergebnis festgehalten", "erfolg"); entwurfsSeite(ziel, gesendet); } }))));
  };
  rc(eintraege, ...liste.map((e) => el("button", { type: "button", class: "listeneintrag", "data-id": e.id, onclick: () => lade(e.id) },
    el("div", {}, e.name), el("div", { class: "gedimmt klein" }, e.status_text + (e.gesendet_am ? " · " + e.gesendet_am.slice(0, 10) : "")))));
  rc(ziel, el("div", { class: "zweispaltig zweispaltig-umgekehrt" },
    karte(null, liste.length ? rechts : el("p", { class: "gedimmt" }, gesendet ? "Noch nichts gesendet." : "Keine offenen Entwürfe. Erstelle einen unter „Firmen“.")),
    karte(gesendet ? "Gesendet" : "Entwürfe", eintraege)));
  if (liste.length) lade(liste[0].id);
}
SEITEN.entwuerfe = (ziel) => entwurfsSeite(ziel, false);
SEITEN.gesendet = (ziel) => entwurfsSeite(ziel, true);

SEITEN.fakten = async (ziel) => {
  const d = await api("fakten");
  const fragen = d.fragen.length ? (() => {
    const f = d.fragen[0];
    const antwort = el("input", { placeholder: f.beispiel, maxlength: 5000 });
    return karte(`Frage ${1} von ${d.fragen.length}: ${f.titel}`, el("p", {}, f.frage), antwort, el("div", { class: "reihe" },
      knopf("Antworten", async () => { if (await versuche(() => api("fragen/" + encodeURIComponent(f.schluessel), "POST", { text: antwort.value }))) zeige("fakten"); }, "primaer"),
      knopf("Überspringen", async () => { if (await versuche(() => api(`fragen/${encodeURIComponent(f.schluessel)}/ueberspringen`, "POST", {}))) zeige("fakten"); })));
  })() : null;
  const kat = el("select", {}, d.kategorien.map((k) => el("option", { value: k, selected: k === "praktisch" ? "selected" : null }, k)));
  const neu = el("input", { placeholder: "Eigenen Fakt ergänzen, gilt sofort", maxlength: 2000 });
  const marke = { vorgeschlagen: ["offen", "orange"], bestaetigt: ["bestätigt", "gruen"], abgelehnt: ["abgelehnt", ""] };
  rc(ziel, fragen || el("span"), karte("Fakten",
    el("div", { class: "reihe" }, kat, neu, knopf("Ergänzen", async () => { if (await versuche(() => api("fakten", "POST", { kategorie: kat.value, text: neu.value }))) zeige("fakten"); }, "primaer")),
    el("div", { class: "reihe" },
      knopf("Fakten aus Unterlagen vorschlagen", () => auftrag("fakten/vorschlagen", {}, (n) => { toast(`${n} Vorschläge`, "erfolg"); zeige("fakten"); }), "blau"),
      knopf("Alle KI-Vorschläge bestätigen", async () => { const r = await versuche(() => api("fakten/alle_bestaetigen", "POST", {})); if (r) { toast(`${r.anzahl} bestätigt`, "erfolg"); zeige("fakten"); } })),
    tabelle(["Status", "Kategorie", "Fakt", "Quelle", ""], d.fakten.map((f) => ({ zellen: [
      el("span", { class: "marke " + marke[f.status][1] }, marke[f.status][0]), f.kategorie, f.text, f.quelle,
      el("span", { class: "reihe" },
        f.status !== "bestaetigt" ? knopf("✓", async () => { if (await versuche(() => api(`fakten/${f.id}/bestaetigen`, "POST", {}))) zeige("fakten"); }) : null,
        f.status !== "abgelehnt" ? knopf("✕", async () => { if (await versuche(() => api(`fakten/${f.id}/ablehnen`, "POST", {}))) zeige("fakten"); }) : null,
        knopf("Ändern", () => {
          const t = el("textarea", { maxlength: 2000 }, f.text);
          dialog("Fakt ändern", t, [knopf("Speichern", async () => { if (await versuche(() => api("fakten/" + f.id, "PUT", { text: t.value }))) { schliesseDialog(); zeige("fakten"); } }, "primaer"), knopf("Abbrechen", schliesseDialog)]);
        }))] })))));
};

SEITEN.profil = async (ziel) => {
  const p = await api("profil");
  const f = { name: el("input", { value: p.name, maxlength: 120 }), email: el("input", { value: p.email, maxlength: 254 }),
    telefon: el("input", { value: p.telefon, maxlength: 60 }), ort: el("input", { value: p.ort, maxlength: 120 }),
    links: el("input", { value: p.links, maxlength: 500 }), vorlage: el("textarea", { maxlength: 20000 }, p.vorlage),
    signatur: el("textarea", { maxlength: 2000 }, p.signatur), woertlich: el("input", { type: "checkbox", checked: p.woertlich }),
    ohne_alter: el("input", { type: "checkbox", checked: p.ohne_alter }) };
  const art = el("select", {}, Object.entries(p.arten).map(([k, v]) => el("option", { value: k }, v)));
  const anhang = el("input", { type: "checkbox", checked: true });
  const datei = el("input", { type: "file", accept: ".pdf,.docx,.txt,.md" });
  rc(ziel, el("div", { class: "raster" },
    karte("Absender", el("div", { class: "formular" }, el("label", {}, "Name"), f.name, el("label", {}, "Mailadresse"), f.email,
      el("label", {}, "Telefon"), f.telefon, el("label", {}, "Wohnort"), f.ort, el("label", {}, "Links"), f.links)),
    karte("Stil", el("div", { class: "spalte" }, el("label", {}, "Stilvorlage"), f.vorlage, el("label", {}, "Signatur"), f.signatur,
      el("label", { class: "reihe" }, f.woertlich, "Vorlage wörtlich verwenden"),
      el("label", { class: "reihe" }, f.ohne_alter, "Alter nicht über Berufsjahre oder Jahreszahlen verraten"),
      knopf("Speichern", async () => {
        const daten = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.type === "checkbox" ? v.checked : v.value]));
        if (await versuche(() => api("profil", "PUT", daten))) { toast("Gespeichert", "erfolg"); ladeIch(); }
      }, "primaer")))),
  karte("Unterlagen",
    tabelle(["Titel", "Art", "Anhang", "Datei", ""], p.unterlagen.map((u) => ({ zellen: [u.titel, p.arten[u.art] || u.art,
      el("input", { type: "checkbox", checked: u.anhang, onchange: (ev) => versuche(() => api(`unterlagen/${u.id}/anhang`, "POST", { anhang: ev.target.checked })) }),
      u.da ? el("span", { class: "ok" }, "vorhanden") : el("span", { class: "hinweis", title: "Liegt nur auf dem Rechner, auf dem sie hinzugefügt wurde. Hier erneut hochladen." }, "fehlt hier"),
      knopf("Entfernen", async () => { if (confirm(`${u.titel} entfernen?`) && await versuche(() => api("unterlagen/" + u.id, "DELETE"))) zeige("profil"); }, "gefahr")] }))),
    el("div", { class: "reihe" }, datei, art, el("label", { class: "reihe" }, anhang, "als Anhang"), knopf("Hochladen", () => {
      if (!datei.files.length) return toast("Wähle eine Datei.", "fehler");
      const fd = new FormData();
      fd.append("datei", datei.files[0]);
      fd.append("art", art.value);
      fd.append("anhang", anhang.checked ? "true" : "false");
      auftrag("unterlagen", fd, (e) => { toast((e.probleme || []).join("\n") || "Eingelesen", (e.probleme || []).length ? "fehler" : "erfolg"); zeige("profil"); });
    }, "primaer"))));
};

SEITEN.mailkonto = async (ziel) => {
  const m = await api("mailkonto");
  const k = m.konto || { email: Z.ich.konto.email, host: "", port: 587, sicherheit: "starttls", benutzer: "" };
  const f = { email: el("input", { value: k.email, maxlength: 254 }), host: el("input", { value: k.host, placeholder: "smtp.example.org", maxlength: 253 }),
    port: el("input", { type: "number", value: k.port, min: 1, max: 65535 }),
    sicherheit: el("select", {}, [["starttls", "STARTTLS"], ["ssl", "SSL/TLS"]].map(([c, n]) => el("option", { value: c, selected: c === k.sicherheit ? "selected" : null }, n))),
    benutzer: el("input", { value: k.benutzer, placeholder: "Meist die Mailadresse", maxlength: 254 }),
    passwort: el("input", { type: "password", autocomplete: "new-password", placeholder: m.passwort_hinterlegt ? "hinterlegt – leer lassen, um es zu behalten" : "Mailpasswort oder App-Passwort", maxlength: 256 }) };
  rc(ziel, karte("Versandkonto", el("p", { class: "gedimmt" },
    "Gesendet wird nur über dein eigenes Konto und nur verschlüsselt (SSL/TLS oder STARTTLS). Das Passwort liegt verschlüsselt in deinem Bereich der Datenbank und wird nie angezeigt. Höchstens 5 Freigaben am Tag."),
  el("div", { class: "formular" }, el("label", {}, "Absenderadresse"), f.email, el("label", {}, "SMTP-Server"), f.host, el("label", {}, "Port"), f.port,
    el("label", {}, "Sicherheit"), f.sicherheit, el("label", {}, "Benutzer"), f.benutzer, el("label", {}, "Passwort"), f.passwort),
  el("div", { class: "reihe" }, knopf("Speichern", async () => {
    const daten = { email: f.email.value, host: f.host.value, port: Number(f.port.value), sicherheit: f.sicherheit.value, benutzer: f.benutzer.value, passwort: f.passwort.value || null };
    if (await versuche(() => api("mailkonto", "PUT", daten))) { toast("Gespeichert", "erfolg"); zeige("mailkonto"); }
  }, "primaer"), knopf("Testmail an mich", () => auftrag("mailkonto/test", {}, (e) => toast("Testmail an " + e.an + " gesendet", "erfolg"))))));
};

SEITEN.system = async (ziel) => {
  const s = await api("system");
  const mb = (b) => (b / 1048576).toFixed(1).replace(".", ",") + " MB";
  rc(ziel, el("div", { class: "raster" },
    karte("Daten", el("div", { class: "werte" }, el("span", { class: "gedimmt" }, "Speicher"), el("span", {}, s.speicher),
      el("span", { class: "gedimmt" }, "Größe"), el("span", {}, mb(s.groesse)),
      Object.entries(s.tabellen).flatMap(([t, n]) => [el("span", { class: "gedimmt" }, t), el("span", {}, n)]))),
    karte("KI", el("div", { class: "werte" }, el("span", { class: "gedimmt" }, "Gewählt"), el("span", {}, s.modell),
      el("span", { class: "gedimmt" }, "Claude"), el("span", {}, s.claude_erlaubt ? el("span", { class: "marke gruen" }, "freigegeben") : "nicht freigegeben – es läuft das lokale Modell (Ollama)"),
      el("span", { class: "gedimmt" }, "Gelernt"), el("span", {}, `${s.lernen.regeln} Regeln, ${s.lernen.korrekturen} Korrekturen, ${s.lernen.bewertungen} Bewertungen`)),
    knopf("KI wählen", () => zeige("konto"), "blau")),
    karte("Sicherung", el("p", { class: "gedimmt" }, s.sicherung),
      knopf("Daten exportieren (JSON)", async () => {
        const r = await versuche(() => api("system/export", "GET", undefined, true));
        if (!r || !r.ok) return;
        const blob = await r.blob();
        const a = el("a", { href: URL.createObjectURL(blob), download: (r.headers.get("Content-Disposition") || "").split('"')[1] || "export.json" });
        document.body.append(a); a.click(); a.remove();
      }, "primaer")),
    karte("Version", el("p", {}, "Job Agent " + s.version))));
};

SEITEN.konto = async (ziel) => {
  const ki = await api("konto/ki");
  const alt = el("input", { type: "password", autocomplete: "current-password", placeholder: "Bisheriges Passwort" });
  const neu = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort (mind. 12 Zeichen)" });
  const neu2 = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort wiederholen" });
  const modelle = ki.modelle.includes(ki.modell) ? ki.modelle : [ki.modell, ...ki.modelle];
  const wahl = el("select", {}, modelle.map((m) => el("option", { value: m, selected: m === ki.modell ? "selected" : null }, m.startsWith("claude:") ? m.slice(7) + " (Claude)" : m + " (lokal)")));
  const ich = Z.ich;
  rc(ziel, el("div", { class: "raster" },
    karte("Konto", el("div", { class: "werte" }, el("span", { class: "gedimmt" }, "Adresse"), el("span", {}, ich.konto.email),
      el("span", { class: "gedimmt" }, "Mandant"), el("span", {}, ich.mandant.name), el("span", { class: "gedimmt" }, "Rolle"), el("span", {}, ich.konto.admin ? "Administrator" : "Nutzer")),
      ich.mandanten.length > 1 ? el("div", { class: "reihe" }, el("span", { class: "gedimmt" }, "Wechseln:"), ich.mandanten.map((m) => knopf(m.name, async () => {
        if (await versuche(() => api("mandant", "POST", { id: m.id }))) { await ladeIch(); zeige("start"); }
      }, m.id === ich.mandant.id ? "primaer" : ""))) : null,
      knopf("Abmelden", abmelden, "gefahr")),
    karte("Passwort ändern", el("div", { class: "spalte" }, alt, neu, neu2, knopf("Ändern", async () => {
      if (neu.value !== neu2.value) return toast("Die neuen Passwörter sind verschieden.", "fehler");
      if (await versuche(() => api("konto/passwort", "POST", { alt: alt.value, neu: neu.value }))) { alt.value = neu.value = neu2.value = ""; toast("Passwort geändert, andere Sitzungen sind abgemeldet", "erfolg"); }
    }, "primaer"))),
    karte("KI", el("p", { class: "gedimmt" }, ki.claude_erlaubt ? "Claude ist für diesen Mandanten freigegeben (Unterlagen gehen dabei an Anthropic)."
      : "Für diesen Mandanten läuft das lokale Modell (Ollama). Claude gibt nur der Betreiber frei."),
    ki.ollama_problem ? el("p", { class: "hinweis" }, ki.ollama_problem) : null,
    el("div", { class: "reihe" }, wahl, knopf("Übernehmen", async () => { if (await versuche(() => api("konto/ki", "POST", { modell: wahl.value }))) { toast("KI gewählt", "erfolg"); ladeIch(); } }, "primaer"))),
    karte("Mailkonto", el("p", { class: "gedimmt" }, "Für den Versand an Firmen. Unabhängig von deinem Konto-Passwort."), knopf("Mailkonto einrichten", () => zeige("mailkonto")))));
};

SEITEN.admin = async (ziel) => {
  const a = await api("admin");
  const notiz = el("input", { placeholder: "Notiz, z. B. für wen", maxlength: 200 });
  const mandant = el("select", {}, el("option", { value: "" }, "eigener neuer Mandant"), a.mandanten.map((m) => el("option", { value: m.id }, "Mitglied in: " + m.name)));
  rc(ziel, el("div", { class: "spalte" },
    karte("Einladung erzeugen", el("p", { class: "gedimmt" }, "Registrieren geht nur mit Code. Der Code wird nur jetzt einmal angezeigt und gilt 7 Tage."),
      el("div", { class: "reihe" }, notiz, mandant, knopf("Erzeugen", async () => {
        const r = await versuche(() => api("admin/einladungen", "POST", { notiz: notiz.value, mandant_id: mandant.value ? Number(mandant.value) : null }));
        if (r) dialog("Einladungscode", [el("p", {}, `Einmalig gültig, ${r.gueltig_tage} Tage:`), el("div", { class: "code" }, r.code)], [knopf("Fertig", () => { schliesseDialog(); zeige("admin"); }, "primaer")]);
      }, "primaer")),
      tabelle(["Notiz", "Erstellt", "Gültig bis", "Benutzt", ""], a.einladungen.map((e) => ({ zellen: [e.notiz, (e.erstellt || "").slice(0, 16), (e.gueltig_bis || "").slice(0, 16),
        e.benutzt_am ? el("span", { class: "ok" }, e.benutzt_am.slice(0, 16)) : "–",
        e.benutzt_am ? "" : knopf("Zurückziehen", async () => { if (await versuche(() => api("admin/einladungen/" + e.id, "DELETE"))) zeige("admin"); })] })))),
    karte("Mandanten", tabelle(["Name", "Konten", "Nachtlauf", "Claude", ""], a.mandanten.map((m) => ({ zellen: [m.name, m.konten.join(", "),
      m.nachtlauf ? m.nachtlauf_uhrzeit : "aus", m.claude_erlaubt ? el("span", { class: "marke gruen" }, "erlaubt") : "nur Ollama",
      knopf(m.claude_erlaubt ? "Claude sperren" : "Claude freigeben", async () => {
        if (!m.claude_erlaubt && !confirm(`Claude für „${m.name}“ freigeben? Anfragen laufen dann über dein Claude-Abo und Unterlagen gehen an Anthropic.`)) return;
        if (await versuche(() => api(`admin/mandanten/${m.id}/claude`, "POST", { an: !m.claude_erlaubt }))) zeige("admin");
      })] })))),
    karte("Konten", tabelle(["Adresse", "Name", "Admin", "Aktiv", "Letzte Anmeldung", ""], a.konten.map((k) => ({ zellen: [k.email, k.name, k.admin ? "ja" : "",
      k.aktiv ? "ja" : el("span", { class: "fehler" }, "nein"), (k.letzte_anmeldung || "").slice(0, 16),
      k.id === Z.ich.konto.id ? "" : knopf(k.aktiv ? "Deaktivieren" : "Aktivieren", async () => { if (await versuche(() => api(`admin/konten/${k.id}/aktiv`, "POST", { an: !k.aktiv }))) zeige("admin"); })] }))))));
};

// --- Start -------------------------------------------------------------------------------------------------------------
$("dialog").addEventListener("click", (ev) => { if (ev.target === $("dialog")) schliesseDialog(); });
starteApp().catch(() => zeigeAnmeldung());
