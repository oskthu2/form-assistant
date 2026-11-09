# form-assistant

En AI-stödd webbklient som gör om ett formulär – en eller några bilder, eller en PDF – till en
**FHIR R4 Questionnaire** som kan laddas upp i en formulärtjänst, till exempel Ineras nationella
formulärtjänst. Du får tillbaka en **FHIR-Bundle** (transaktion) med formuläret och en
**Provenance** som visar att AI:n var författare.

```
 bilder / PDF ─► igenkänning ──känt──► kopia (derivedFrom)
                    │
                    └─nytt─► Claude tolkar ─► utkast ─► iterera ─► godkänn ─► FHIR-Bundle
                                 ▲                        │           │
              lärdomar + exempel + urvalsbibliotek ◄──────┴───────────┘
```

## Kom igång

```bash
pip install -e '.[dev]'
export ANTHROPIC_API_KEY=…                                   # eller Codespaces-hemlighet
export FORM_CANONICAL_BASE=https://fhir.example-region.se/formular
form-assistant                                               # http://127.0.0.1:8000
pytest -q
```

### GitHub Codespaces

1. Claude-nyckeln är en Codespaces-hemlighet på ditt konto, och den når bara de repon den har
   åtkomst till. Har du redan `ANTHROPIC_API_KEY` för ett annat repo: gå till **GitHub → Settings
   → Codespaces → Secrets → ANTHROPIC_API_KEY → Repository access** och lägg till
   `form-assistant`. Annars skapar du hemligheten där och ger den åtkomst till repot.
2. Skapa en codespace på grenen. `.devcontainer` installerar och kör testerna. Hemligheten läses
   när codespacen startar, så en codespace som redan kör behöver startas om efter steg 1.
3. Kör `form-assistant --host 0.0.0.0` och öppna port 8000. Låt porten vara privat: i Codespaces
   körs verktyget i demoläge, där inloggningen bara består av ett namn.

| Miljövariabel | Betydelse |
|---|---|
| `FORM_DB` | SQLite-databasen (standard `data/form-assistant.db`) |
| `FORM_MODEL` | Claude-modell (standard `claude-opus-5-5`) |
| `FORM_CANONICAL_BASE`, `FORM_PUBLISHER`, `FORM_ALLOWED_EXTENSIONS`, `FORM_ANSWER_OPTIONS` | värden för det förvalda formulärekosystemet "standard", som skapas första gången (se nedan). Därefter förvaltas ekosystemen i webbklienten |

## Två lägen

| | Öppet läge | Inloggat läge (arbetsytan) |
|---|---|---|
| Vem | vem som helst, utan inloggning | inloggade användare |
| Gör | laddar upp bilder eller PDF, förhandsvisar och hämtar Questionnaire eller Bundle | allt nedan: iteration med AI:n, urvalsbibliotek, lärdomar, ekosystem, godkännande, versioner |
| Igenkänning | mot **godkända** formulär, som kan hämtas direkt utan AI-tolkning | mot alla formulär i arbetsytan, med kopia |
| Lagring | resultatet nås via en hemlig länk och raderas med filerna efter `FORM_OPEN_RETENTION_HOURS` (24 h) | beständigt |
| Träning | används inte som exempel eller återkoppling | godkända formulär och instruktioner förbättrar prompten |

Det öppna läget skyddas på flera sätt. Varje tolkning är ett AI-anrop, så antalet är begränsat per
klientadress och timme (`FORM_OPEN_PER_HOUR`, standard 5) och totalt per dygn
(`FORM_OPEN_PER_DAY`, 100). Även antalet filer (`FORM_OPEN_MAX_FILES`, 5) och sidor per PDF
(`FORM_OPEN_MAX_PAGES`, 30) är begränsat. Klientadressen sparas bara som en hash. Öppna resultat
syns aldrig i arbetsytan. Uppladdningarna refereras med signerade id:n, så ingen kan be AI:n tolka
någon annans fil. En inloggad användare kan välja **Ta in i arbetsytan** på ett öppet resultat och
arbeta vidare med det. Med `FORM_OPEN=0` stängs det öppna läget av.

### Inloggning (`FORM_AUTH`)

| Läge | Inloggning |
|---|---|
| `demo` (standard) | man anger bara sitt namn. Ingen riktig inloggning, och läget är spärrat med `FORM_PRODUCTION=1` |
| `oidc` | OpenID Connect mot organisationens IdP (Entra ID, Keycloak, Ineras IdP). Flödet använder authorization code med PKCE, `state` och `nonce`, och ID-tokenet valideras mot IdP:ns nycklar (JWKS) med kontroll av utgivare, mottagare och giltighetstid. Bara asymmetriska algoritmer godtas. Variabler: `FORM_OIDC_ISSUER`, `FORM_OIDC_CLIENT_ID`, `FORM_OIDC_CLIENT_SECRET`, `FORM_SESSION_SECRET` (minst 32 tecken) och `FORM_PUBLIC_URL` |
| `proxy` | en inloggningsproxy framför verktyget skickar användaren i `X-Forwarded-User`. Huvudet godtas bara från `FORM_TRUSTED_PROXIES` och, om det är satt, bara med `FORM_PROXY_SECRET` |

`FORM_ALLOWED_GROUPS` begränsar arbetsytan till vissa grupper från IdP:n eller proxyn. Med
`FORM_PRODUCTION=1` startar verktyget inte med osäkra inställningar: demoläge, http mot IdP:n,
kort sessionsnyckel eller en proxy som litar på alla adresser. Mer detaljer står i
`src/form_assistant/auth.py`.

## Så fungerar det (inloggat läge)

1. **Ladda upp** bilder (en per sida) eller en PDF, gärna med en kort beskrivning.
2. **Igenkänning ("shazam").** Innan något AI-anrop görs jämförs underlaget med formulären som
   redan finns. Om det är samma blankett erbjuds en **kopia**. Kopian utgår från det befintliga
   formuläret med dess frågor och urval, och får `derivedFrom` och en Provenance med
   `derivation`.
3. **Tolkning.** Annars tolkar Claude formuläret till ett strukturerat utkast. Det innehåller
   frågor, typer, numrering, obligatoriska fält, villkor, svarsalternativ och vad frågor om
   personnummer, namn eller organisation betyder. Osäkerheter och iakttagelser visas.
   Tolkningen körs som ett bakgrundsjobb. Ett stort formulär, med tiotals sidor och hundra
   frågor, kan ta flera minuter. En statussida visar hur många frågor Claude har beskrivit och
   går vidare till formuläret när det är klart, så inget anrop hänger tills en proxy ger 504.
   Svaret strömmas med en gräns på 64 000 tokens.
4. **Iteration.** Förhandsvisningen visar formuläret som det blir i FHIR. Instruera AI:n ("gör
   fråga 3 obligatorisk", "flera allergier ska kunna väljas"). Varje version sparas och kan
   återställas.
5. **Urval.** Varje flervalsfråga jämförs med urvalsbiblioteket. Du får förslag på befintliga
   urval med samma alternativ, eller med alla alternativ och fler, och byter med ett klick.
6. **Godkännande.** Urvalen sparas i biblioteket, eller kopplas till likadana befintliga urval.
   Formuläret blir `active` och används som exempel för kommande tolkningar.
7. **Export.** Du laddar ner Bundlen: Questionnaire (`PUT`), eventuella ValueSet och CodeSystem
   (`PUT`) och Provenance (`POST`).

## Formulärekosystem

Ett formulärekosystem är den formulärtjänst eller organisation som formulären skapas för, till
exempel Ineras formulärtjänst eller en region. Ekosystemet bestämmer:

- canonical-basen för formulärets och de nya urvalens URL:er
- utgivaren
- hur svarsalternativen uttrycks: `answerOption` i formuläret, eller `answerValueSet` med
  ValueSet och CodeSystem i bundlen
- vilka extensions tjänsten stöder

Ekosystemen lagras som ett **urval** och kan hämtas som FHIR CodeSystem (med egenskaperna
`canonical-base`, `publisher`, `answer-options`, `allowed-extension` och `default`) och
ValueSet. Ekosystemet väljs i en vallista när formuläret laddas upp och kan bytas på
formulärsidan. En kopia kan skapas för ett annat ekosystem än originalet.

Första gången skapas ekosystemet **standard** från miljövariablerna ovan och blir förvalt.
Under **Ekosystem** lägger du till fler, ändrar dem och väljer vilket som är förvalt. Ett
ekosystem med en exempeladress (`example.org`) som bas får en varning, eftersom adressen måste
bytas innan formulär laddas upp i en riktig tjänst.

## Kompatibilitet med Ineras formulärtjänst – få extensions

Ineras IG för Questionnaire och QuestionnaireResponse
([Inera FHIR Forms](https://inera-ab.github.io/fhir-portal/)) är aviserad men inte publicerad.
Verktyget håller sig därför till element i **FHIR R4 core** och använder som standard **inga
extensions**:

| Behov | Så uttrycks det (core) |
|---|---|
| frågetyp, obligatorisk, flera svar, skrivskyddad | `type`, `required`, `repeats`, `readOnly` |
| numrering | `prefix` |
| längd | `maxLength` |
| svarsalternativ | `answerOption.valueCoding`, eller `answerValueSet` |
| villkor ("om ja …") | `enableWhen` + `enableBehavior` |
| hjälptext | ett underordnat `display`-item (i stället för extensionen `itemControl`/help) |
| koppling till patient och organisation | `item.definition` (definitionsbaserad extraktion) |

Det AI:n beskriver görs om till FHIR av koden, inte av AI:n. Därför följer resultatet reglerna
oavsett vad modellen svarar, och det som måste anpassas visas som varningar. När det är klart vilka
extensions formulärtjänsten stöder läggs de till på ekosystemet. Med
`http://hl7.org/fhir/StructureDefinition/regex` får till exempel personnummer, HSA-id och
organisationsnummer ett valideringsmönster.

## Personnummer och organisation enligt svenska basprofiler

Frågor som avser patienten eller vårdgivaren pekar med `item.definition` på elementet i HL7
Sveriges [basprofiler](https://build.fhir.org/ig/HL7Sweden/basprofiler-r4/) (SEBasePatient 1.1.0,
SEBaseOrganization 1.1.0). Svaren kan då föras över till resurser enligt profilerna:

| Fråga | `item.definition` | Identifierare i profilen |
|---|---|---|
| personnummer | `SEBasePatient#Patient.identifier:personnummer.value` | `http://electronichealth.se/identifier/personnummer` |
| samordningsnummer | `SEBasePatient#Patient.identifier:samordningsnummer.value` | `http://electronichealth.se/identifier/samordningsnummer` |
| förnamn, efternamn, namn | `SEBasePatient#Patient.name.given` / `.family` / `.text` | |
| telefon, e-post, adress | `SEBasePatient#Patient.telecom.value`, `Patient.address.*` | |
| mottagning/vårdgivare | `SEBaseOrganization#Organization.name` | |
| HSA-id | `SEBaseOrganization#Organization.identifier:hsaid.value` | `urn:oid:1.2.752.29.4.19` (typ PRN) |
| organisationsnummer | `SEBaseOrganization#Organization.identifier:organizationIdentifier.value` | `urn:oid:2.5.4.97` (typ XX) |

Personnummer och samordningsnummer blir alltid `string`, med `maxLength` 13, så att formatet
ÅÅÅÅMMDDNNNN, med eller utan bindestreck, inte tappar inledande nollor.

## AI:ns författarskap – Provenance

Bundlen innehåller en Provenance för formuläret:

- **författare:** modellen som en inbäddad Device, till exempel `AI: claude-opus-5-5`
- **registrerare:** de personer som laddade upp underlaget och instruerade AI:n
- **granskare:** den som godkände formuläret
- **underlag (`entity`):** de uppladdade filerna med SHA-256, varje instruktion vid iteration och,
  för en kopia, ursprungsformuläret (`derivation`)

Bundlarna validerar utan fel mot FHIR R4 med HL7-validatorn, när ekosystemet har en riktig
canonical-bas.

## Urvalsbiblioteket

Svarsalternativ sparas som ValueSet med tillhörande CodeSystem när ett formulär godkänns.
Biblioteket används på tre sätt:

- **I prompten:** AI:n får biblioteket och sätter `value_set` när ett befintligt urval motsvarar
  formulärets alternativ, gärna det med mer standardiserad formulering.
- **Vid granskning:** varje flervalsfråga jämförs med biblioteket. Jämförelsen tål skrivsätt och
  numrering. Förslagen är *samma alternativ*, *täcker alla alternativ och fler*, eller
  *närliggande*, sorterade efter hur väl de stämmer och hur många formulär som använder dem.
- **Vid godkännande:** ett likadant urval återanvänds i stället för att ett nytt skapas, och
  biblioteket räknar hur många formulär som använder varje urval.

## Formulär-shazam

Igenkänningen använder fyra signaler, från den billigaste och säkraste till den bredaste:

1. **Samma fil:** SHA-256.
2. **Samma blankett i bild:** perceptuell hash (dHash, 64 bitar) av bilden eller av första sidan
   i en skannad PDF. Den tål ny skanning, annan upplösning och komprimering, men inte ett annat
   formulär. En PDF med textlager avgörs av texten, eftersom första sidan ofta är ett
   försättsblad som många dokument delar.
3. **Samma text:** fingeravtryck av hashade ordtripplar i PDF:ens textlager eller i ett tolkat
   formulärs frågor, jämförda med Jaccard-likhet. Det kontrolleras också efter tolkningen, så att
   formulärsidan kan tipsa om ett nästan likadant formulär.
4. **Samma beskrivning:** fritextsökning ("hälsodeklaration vaccination") över titlar,
   beskrivningar och frågor, BM25-viktad.

Ett känt formulär kostar alltså inget AI-anrop: du får välja mellan en kopia och att tolka ändå.

## "Träning" av instruktionsprompten

Alla formulär, bilder och versioner sparas i databasen. Instruktionsprompten förbättras på två
sätt:

- **Lärdomar.** Instruktionerna som ges vid iteration samlas som återkoppling. Under *Lärdomar*
  kan AI:n föreslå allmänna lärdomar ur återkopplingen, och du kan skriva egna. Bara de som
  godkänns läggs till i systemprompten.
- **Exempel.** Godkända formulär som liknar det nya, efter beskrivningen eller de senast godkända,
  skickas med som exempel.

Systemprompten och de uppladdade filerna cachas (prompt caching), så att en revidering av samma
formulär läser bilderna ur cachen. Server-side fallback är påslaget: om modellen avböjer en
begäran tar en annan modell över i samma anrop.

## Struktur

```
src/form_assistant/
  model.py      formulärutkastet (pydantic) – schema för Claude och lagring
  ai.py         tolkning, revidering och förslag på lärdomar (Claude, strukturerad output)
  fhir.py       utkast -> Questionnaire, ValueSet/CodeSystem, Provenance, Bundle
  valuesets.py  urvalsbiblioteket: jämförelse, förslag, sparande
  ecosystems.py formulärekosystemen (lagras som ett urval)
  auth.py       inloggning för arbetsytan: demo, OpenID Connect, proxy
  shazam.py     igenkänning: filhash, bildhash, textavtryck, sökning
  service.py    arbetsflödet
  db.py         SQLite: uppladdningar, formulär, versioner, urval, lärdomar
  web.py        webbklienten (Starlette + Jinja2)
tests/          tester med fejkad Claude-API och syntetiska blanketter
```

Formulär som postas från andra webbplatser avvisas, och sidorna får inte bäddas in i andra sidor.
Både det öppna läget och arbetsytan använder organisationens Claude-nyckel. Gränserna i det öppna
läget håller nere kostnaden; stäng av det med `FORM_OPEN=0` om verktyget bara ska användas internt.
