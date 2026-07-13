# Fix-Plan zum Audit 2026-07-12

Status: ENTWURF — noch nicht umgesetzt. Referenzen (A1…B12) → AUDIT-2026-07-12.md.

Produktentscheidungen von Alexander (2026-07-13):
- Laufende Events: **Overlap-Semantik als Default** (ongoing:true-Flag).
- Datum-ohne-Zeit: **Zeit aktiv ermitteln** — erst tiefer nachschauen
  (Detailseite), sonst LLM-Schätzung mit Confidence, klar gelabelt.
- Organizer: **Ja** — Spalte + Suche + Backfill.
- Geo: **alles behalten**, Queries bekommen einen Default-Radius um Linz.

Architektur-Hebel, der fast alles trägt: **Canon ist eine pure Funktion der
Claims** (rebuild löscht occurrence+event und baut neu, rebuild.py:723-724).
Fixes, die im Rebuild-Pfad sitzen (statt nur in der Extraktion), reparieren
den gesamten Bestand beim nächsten `resolve` — kein Backfill nötig.
Append-only-Kontrakte (event_claim, identity) werden nirgends verletzt;
neue Spalten auf event/occurrence sind zulässig, werden aber im
DECISIONS-Changelog vermerkt.

---

## Block 1 — Venue-Resolver (A1, A5-GrooveIsland, A13, A17, B8)

Ort: `resolve/venues.py` (VenueResolver), `resolve/rebuild.py:157-175`.
Der Schneeball ist dokumentierte Policy („aggressive alias growth",
venues.py:3-4) + `word_similarity('Linz','JKU Linz')=1.0` + max-über-Aliases
+ Alias-Rückschreibung bei jedem Fuzzy-Match (venues.py:56-63). Die
venue-Tabelle überlebt Rebuilds → monotone Vergiftung.

1. **Fuzzy nur gegen `venue.name`** — max-über-alle-Aliases aus dem Score
   entfernen (venues.py:44-56). Exakte Alias-Treffer bleiben.
2. **Symmetrie erzwingen**: Fuzzy-Treffer brauchen zusätzlich
   `similarity() >= 0.55`; word_similarity allein reicht nie.
3. **Kein Auto-Alias bei Fuzzy-Match** (venues.py:56-63 löschen). Aliases
   entstehen nur noch über den adjudizierten <300-m-Pfad
   (`_reconcile_venues`, rebuild.py:415-442) oder Review.
4. **Generik-Stoplist** vor `resolve()` (neue kleine Funktion in venues.py):
   nackte Orts-/Distriktnamen („Linz", „Innenstadt", „Stadt Linz",
   „Oberösterreich", „Online"), reine Adressen (`_STREET_RE` rebuild.py:893
   wiederverwenden), Einzel-Generika („Großer Saal" ohne Kontext) → weder
   Venue noch Alias; Claim behält venue_name=NULL (null=unknown statt falsch).
5. **Geo-Veto + Geo-Backfill**: Fuzzy-Match ablehnen, wenn Claim- und
   Venue-Geo >`VENUE_ALIAS_MAX_M` (300 m) auseinander; umgekehrt
   `venue.geo` per UPDATE backfillen, wenn NULL und der Claim
   JSON-LD-Koordinaten hat (venues.py:66-76 / `_lookup`). Senkt die
   84 %-NULL-Quote laufend. (Geocoding-Dienst = neue Infrastruktur = bewusst
   NICHT in diesem Plan.)

**Repair:** venue-Tabelle aus Claims neu ableiten (Aliases verwerfen, Venues
ohne Exact-Name-Claim löschen; Original-Strings liegen unveränderlich in
event_claim.payload), dann voller rebuild. Kostenhinweis:
`enrichment.content_key` enthält venue_name → betroffene Events re-enrichen
(bounded; Budget einplanen). **Tests:** neues `tests/test_venues.py`
(Generik-Rejection, kein Fuzzy-Alias, Geo-Veto, Backfill). venues.py-Docstring
(„aggressive alias growth") korrigieren, sonst restauriert der nächste Agent
den Schneeball.

## Block 2 — Fingerprint/Merge/Duplikate (A2a-c, A9, A14-Kulis, A21-Spans, A22-Folge)

Mechanik laut Kartierung: per-Datum-Fingerprints (`fingerprint.py:40-50`),
Serien-Fingerprints mit Wochentag+30-min-Bucket (`recurrence.py:157-164`),
Gruppen-Union nur bei exakter Fingerprint-Gleichheit (rebuild.py:375-412),
Pass-3-Blocking nur same-day (rebuild.py:321-362) → Serie↔Einzeltermin und
Woche-zu-Woche haben KEINEN Merge-Pfad.

1. **Serien-Fingerprint entschärfen** (recurrence.py:164): Wochentag- und
   Zeit-Bucket-Suffix entfernen → Pass-1-Key == Pass-2-Key
   (`series|ntitle|vkey`). Bewusste Folge: zwei gleichnamige Serien am selben
   Venue an verschiedenen Wochentagen kollabieren zu einem Event mit
   Occurrences an beiden Tagen — für diese Daten korrekt. Der Test
   `test_series_fingerprint_tolerates_30min_and_matches_weekday`
   (test_recurrence.py:102) kodiert das alte Verhalten als gewollt und wird
   absichtlich geändert (DECISIONS-Changelog-Eintrag).
2. **Union-Pass Serie↔Einzeltermine** (neu, nach `_group_claims`): jede
   `series|*`-Gruppe absorbiert One-off-Gruppen mit gleichem (ntitle, vkey),
   deren Tag in den beobachteten Serien-Tagen liegt. Löst die
   Sommerkonzerte-6×, Crimetime-3×, Kino-Wochen-Dubletten.
3. **Titel-Normalisierung für Fingerprints** (`fingerprint.py:24-30`):
   Kino-Versionsmarker (omdtu, omu, ov, odf, df, omeu) als Stopwords;
   Venue-Suffix-Strip passiert in Block 4.1 schon am Claim. Voller Titel
   bleibt am Event. Achtung: verschiebt alle neuen Fingerprints —
   `_assign_identity` (rebuild.py:500-547) merged die Alt-Identities beim
   Rebuild automatisch (Survivor = frühestes first_seen); Gold-Set-Lauf
   Pflicht. „Minions" vs „Minions & Monster" bleibt Adjudicator-Fall +
   Prompt-Regel in llm_text._PROMPT („Filmtitel wörtlich übernehmen").
4. **Mitternachts-Phantome falten** (rebuild.py:692-696): `seen`-Key für
   Claims mit `has_time == False` (Heuristik existiert, rebuild.py:86-90)
   ist der lokale TAG; datum-only bestätigt eine getimte Occurrence
   desselben Lokaltags statt sie zu duplizieren; kommt die getimte später,
   ersetzt sie die Mitternachts-Occurrence.
5. **Span-Gate** (gleiche Stelle + rrule_raw-Zweig rebuild.py:682-687):
   `ends_at < starts_at` → ends_at=NULL; Span > 370 Tage → ends_at=NULL
   (nichts Echtes läuft 2 Jahre; Ausstellungen/Kurse bleiben unangetastet,
   die brauchen ihre Spans für die Overlap-Sichtbarkeit aus Block 5).
6. **Kulis-Fall**: Nach 2.+3. treffen sich die widersprüchlichen Termine in
   einer Gruppe; bei >1 behaupteten Termin gleicher (ntitle,vkey) ohne
   Serien-Evidenz → bestehende Adjudication nutzen (kein neuer Mechanismus).

**Repair:** kein Script nötig — Grouping-Fixes deployen, einen `resolve`
laufen lassen; `_assign_identity` vereinigt die Fragmente selbst
(id-Kontinuität: Survivor = frühestes first_seen, bestehende Konvention).
Verifikation mit den Audit-Queries (223-Paare, Mitternachts-Twins → 0).

## Block 3 — Recurrence-Compiler (A2a-RRULE, A3)

1. **Text-Recurrence-Gate pro Claim** (rebuild.py:286-292): eine Regel darf
   nur an Claims, deren Wochentag die Regel erzeugen kann (weekly: Tag ∈
   BYDAY; daily: alle). Sonst ist der Claim ein normaler One-off. Verhindert
   das Stempeln der Do-Regel auf Fr/Sa/So/Mi-Claims.
2. **Beobachtete Termine sind Ground Truth** (`_occurrences_for` Branch 1,
   rebuild.py:665-672): Expansion wird mit den beobachteten Claim-Terminen
   VEREINIGT statt sie zu verwerfen. Die Regel verlängert nur.
3. **DTSTART-Bug** (rebuild.py:668-670): bei leerer Expansion mit `anchor`
   (min Claim-Start) kompilieren statt `now`; Gate rebuild.py:733 wird
   `if not pairs: continue` — nie wieder Event-Zeilen mit RRULE und 0
   Occurrences (unsichtbar waren sie ohnehin; identity bleibt erhalten).
4. **Cache-Purge**: text_recurrence-Einträge löschen, deren Regel den
   Wochentagen der tragenden Claims widerspricht (Sommerkonzerte-Regel
   explizit); Birth-Verify (rebuild.py:258-272) um genau diesen
   Claim-Konsistenz-Check erweitern.

**Repair:** Cache-Purge + Rebuild. Die 115 T080114-Events lösen sich auf
(bekommen echte Occurrences oder verschwinden zugunsten der Claim-Termine).

## Block 4 — Extraktion/Normalisierung (A4-Zeit, A5, A6, A7, A10, A16, A18, A23)

Kartierung: kein gemeinsames Claim-Schema (nur LLMEvent ist typisiert,
llm_text.py:42-56); Sanity = nur `is_upcoming` + `is_placeholder_title`
(extract/__init__.py:42-84); einziges Unescape lebt lokal in
linztermine._clean(). Wichtig: Fixes werden in `sanity_filter` UND
gespiegelt in `_load_claims` (rebuild.py:137-153) eingebaut — Letzteres
repariert die unveränderliche Claim-Historie beim Rebuild gratis.

1. **`clean_text()` zentral** (extract/__init__.py): 2× html.unescape +
   Whitespace-Kollaps + Deko-Strip, angewandt auf String-Payload-Werte in
   `sanity_filter` (vor dem Fingerprinting in handlers.py:104-118!) und in
   `_load_claims`. Dazu Venue-Suffix-Strip: endet der Titel mit venue_name
   → abschneiden (A22, happeningnext-Suffixe, „ - Posthof Linz").
2. **Zeit-Ermittlung statt Mitternacht** (Produktentscheid):
   a) `occurrence.time_unknown bool` (Migration), gesetzt aus der
      has_time-Heuristik beim INSERT (rebuild.py:819-828), in den
      API-Selects exponiert. Das ist die ehrliche Basis.
   b) Follow-up-Job „Detailseite holen, Zeit suchen" für ALLE
      time_unknown-Events mit Zukunfts-Terminen (Produktentscheid
      2026-07-13; ~3.000 initial, dann laufend; Budget-Ring + Rate-Limit,
      bestehende Job-Tabelle — keine neue Infrastruktur).
   c) Bleibt die Zeit unbekannt → LLM-Schätzung im bestehenden Enrichment
      (Priors: Kategorie/Venue-typische Startzeit), validiert (pydantic,
      Zeit-Range-Check), gespeichert als inferred `start_time_estimate`
      {value, confidence}; API zeigt sie als `time_estimated` an, Fenster-
      Matching nutzt sie. starts_at in occurrence bleibt unverfälscht.
3. **Preis-Plausibilisierung** in sanity_filter + _load_claims:
   0 ≤ preis ≤ 500, min ≤ max, Jahreszahl-Heuristik (1900–2100 &
   == Zahl im Titel/Beschreibung) → Feld verwerfen (Kupfermuckn-1840).
4. **Vergangenheits-Gate im Rebuild** (Occurrence-Pair-Filter, nicht
   _load_claims — Serien-Anker braucht min(starts_at)): Pairs älter als
   now − 90 Tage fallen weg; Events ohne verbleibende Pairs übersprungen
   (STWST 2001–2019 verschwindet beim nächsten Rebuild).
5. **Organizer** (Produktentscheid Ja): `ALTER TABLE event ADD COLUMN
   organizer text` + eine Zeile im INSERT-Dict (rebuild.py:~803) —
   FIELD_KEYS/Merge/Provenance können es schon. Zusätzlich `organizer` in
   LLMEvent aufnehmen (LLM-Tier extrahiert es bisher nie). Backfill der
   7.596 Claims = der Rebuild selbst.
6. **Tote Spalten**: `lang` beim Rebuild aus dem Enrichment-Cache kopieren
   (gratis); `tags` aus linztermine `<tag>` (linztermine.py:85, wird heute
   nach dem Category-Mapping verworfen); LLMEvent + FIELD_KEYS + INSERT um
   `registration_required`, `booking_url` erweitern (deterministische Muster
   „Anmeldung erforderlich/erbeten" + JSON-LD offers.url) — wirkt nur für
   künftige Crawls. `drop_in_ok`/`participation_mode`/`doors_at_offset`:
   werden gestrichen (Produktentscheid 2026-07-13; Git erinnert sich).
7. **Nicht-Event-Gate**: Muster-Liste in extract/__init__.py:51-54 erweitern
   (ferien, schulfrei, hinweis auf, öffnungszeiten, „geschlossen"), Spiegel
   in _load_claims; Confidence-Floor für LLM-Payloads (llm_text.py:90-105
   behält heute alles).
8. **Deep-URLs** (A7): (i) Detail-URL als Payload-Feld anhängen, wenn der
   Cascade-Payload keine hat (recipe.py:378-381 kennt `durl`, wirft sie
   weg); (ii) Dedupe bevorzugt URL-tragende Payloads (recipe.py:322-328);
   (iii) Sofortmaßnahme im Rebuild: `url == Quellen-Homepage` → NULL
   (rebuild.py:803), damit die API keine falschen Links behauptet;
   (iv) linztermine-Deep-Recipe re-onboarden (event/<id>-Links einsammeln)
   + Re-Crawl — die Information fehlt in alten Claims wirklich.
9. **happeningnext** (A18): Clickbait-Strip („FAST AUSVERKAUFT" etc.) via
   clean_text-Mustern; source.trust senken (Aggregator hinter Erstquellen).

## Block 5 — Query-Semantik & API (A12, A21, B1–B11, Geo-Entscheid)

Alles in api/search.py + api/app.py, unabhängig von Block 1–4 deploybar.

1. **Overlap-Fenster als Default** (Produktentscheid): build_sql-Fenster wird
   `o.starts_at <= %(to)s AND coalesce(o.ends_at, o.starts_at) >= %(from)s`;
   Rows bekommen `ongoing: true` wenn starts_at < from. Gilt für /v1/query,
   /v1/occurrences, feed.ics, Kalenderseite. llms.md-Fine-print ersetzen.
2. **Default-Radius um Linz = 15 km** (Produktentscheid 2026-07-13): Filter
   `NOT (geo bekannt AND Distanz > 15 km um Linz-Zentrum)` — geo-lose Events
   bleiben drin (null=unknown!). Voll overridable: `near=` + `radius=`
   kommen dafür AUCH auf /v1/query (heute nur auf /v1/occurrences), damit
   „5 km um den Pleschinger See" geht; `radius=any` schaltet das Gate ab.
   llms.txt dokumentiert Default + Override prominent.
3. **`distinct=event`** auf /v1/query: nächste/bestbewertete Occurrence pro
   Event + `occurrence_count`. Default bleibt occurrence-level; llms.md
   empfiehlt distinct für Discovery-Fragen. (B1)
4. **`sort=relevance|starts_at`** (Default relevance = Status quo, endlich
   dokumentiert). (B2)
5. **Kategorie-Validierung**: unbekannte Werte in categories/
   exclude_categories → 422 mit Taxonomie. (B3)
6. **Bindestrich/Kompositum**: Mehrwort-Terme matchen mit `[-\s]?`-Fugen +
   Zusammenschreibung („krone fest" → Krone-Fest/Kronefest). (B4)
7. **`offset`** auf /v1/query (Pool ≤2000) + **include_terms** auf
   /v1/occurrences → erschöpfende Textsuche über Cursor. (B5)
8. **to_dt ohne Zeit = Tagesende** (23:59:59 lokal). (B6)
9. **Cursor base64url** (opaque, URL-safe). (B′)
10. **Row-Felder**: + event_status (tentative!), kind, organizer,
    time_unknown/time_estimated, ongoing. Interne Quellen
    (source.kind='internal') aus provenance_summary filtern (A12).
    RateLimit-Header (nice-to-have).
11. **422 statt still-leer**: age_min>age_max, from>to, min_confidence∉[0,1].

## Block 6 — Betrieb (A20)

1. Digest: eigene Sektion „PRODUKTIVE QUELLE DEGRADIERT" für
   status='degraded' AND gelieferte Events>0 (Alexanders Regel #1).
2. Nach jedem Repair-Rebuild: Gold-Set + Fixture-Replays (Merge-Pflicht),
   Vorher/Nachher-Audit-Zahlen in DECISIONS-Changelog.

## Reihenfolge

1. **Block 1** (Venue) — korrumpiert laufend weiter; braucht den ersten
   Repair-Rebuild.
2. **Block 2+3+4.1/3/4/5/7** zusammen — EIN weiterer Rebuild deckt alle
   Claim-seitigen Repairs ab (Duplikate, RRULE, Phantome, Entities, Preise,
   Archiv, Organizer).
3. **Block 4.2/6/8** — Zeit-Ermittlung, neue Extraktionsfelder,
   linztermine-Re-Crawl (Recipe-Bump).
4. **Block 5** (API) — sofort deploybar, größter sichtbarer Gewinn, kann
   parallel zu 1–3 laufen.
5. **Block 6** — klein, jederzeit.

Jeder Schritt: Tests zuerst (die Kartierung listet die fehlenden:
date-only-Kollision, Tages-Serien, Zero-Occurrence-Serien, Span-Sanity,
Wochen-Kontinuität, Serie↔One-off-Merge), Gold-Set vor Merge, Audit-Queries
als Abnahme gegen Prod.

## Entschiedene Fragen (Alexander, 2026-07-13)

1. Default-Radius: **15 km**, voll overridable (near=/radius= auch auf
   /v1/query; radius=any schaltet ab).
2. Tote Spalten: **streichen** (drop_in_ok, participation_mode,
   doors_at_offset); registration_required + booking_url **echt befüllen**.
3. Zeit-Nachfetch: **alle zukünftigen** time_unknown-Events, nicht nur 30 Tage.
4. Fenster-Semantik: **Overlap als Default** mit ongoing-Flag.
5. Zeit unbekannt: **aktiv ermitteln** (Nachfetch, dann LLM-Schätzung mit
   Confidence, gelabelt).
6. Organizer: **ja** (Spalte, Suche, Backfill via Rebuild).
7. Geo-Scope: **alles behalten**, Default-Radius regelt die Sicht.

Keine offenen Fragen — Plan ist umsetzungsbereit nach Freigabe.
