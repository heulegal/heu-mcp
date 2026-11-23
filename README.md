<!-- mcp-name: io.github.Lucav21/heu-mcp -->

# HEU Legal MCP Server

[![PyPI](https://img.shields.io/pypi/v/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/heu-mcp.svg)](https://pypi.org/project/heu-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP%20Registry-io.github.Lucav21%2Fheu--mcp-blue)](https://registry.modelcontextprotocol.io/v0/servers?search=Lucav21/heu-mcp)

MCP server (Model Context Protocol) che collega l'[API HEU Legal](https://heulegal.com) a Claude e a qualsiasi client MCP. Gestisce l'intero ciclo di vita dei documenti con firma elettronica (valida in 180+ paesi) **direttamente in conversazione**: dalla creazione all'invio in firma, dal sollecito al download del fascicolo legale completo.

---

## Cosa puoi fare

| Voglio... | Il server lo fa con... |
|---|---|
| 📄 Vedere i miei documenti e templates | `list_heu_documents`, `list_pdf_documents` |
| ✍️ Mandare un contratto in firma da un template | `create_heu_document`, `create_pdf_document` |
| 📬 Mandare lo stesso contratto a **centinaia di persone** da un Excel/CSV, in una sola operazione | `create_pdf_documents_bulk` |
| 🚀 Mandare in firma un PDF che ho sul computer, senza passare dalla piattaforma | `create_pdf_document_from_upload` |
| 🤖 Far mappare i campi firma **all'AI** (analizza il PDF, posiziona i campi, invia) | `locate_pdf_text` + `create_pdf_document_from_upload` |
| 🔔 Sollecitare chi non ha ancora firmato | `prompt_heu_document_signature`, `prompt_pdf_document_signature` |
| 👀 Far *leggere* un contratto all'AI (riassunti, clausole, confronti) senza scaricarlo | `read_heu_document`, `read_pdf_document` |
| 🪪 Estrarre i dati delle parti (P.IVA, codice fiscale, SDI, PEC, indirizzi) | `extract_heu_document_parties`, `extract_pdf_document_parties` |
| 💾 Scaricare il PDF firmato | `download_heu_document_pdf`, `download_pdf_document` |
| ⚖️ Scaricare il fascicolo legale completo (documento + audit trail + artefatti FES) | `download_pdf_bundle`, `download_pdf_audit_trail` |
| 🗂️ Scaricare **in blocco** i contratti firmati di una cartella o di un periodo | `download_pdf_bundles_bulk` |
| 🧩 Creare/modificare templates PDF riutilizzabili via API | `create_pdf_template`, `update_pdf_template`, `preview_pdf_template`, `delete_pdf_template` |
| ❌ Annullare una richiesta di firma inviata per errore | `cancel_pdf_document` |
| 🩺 Controllare che l'API sia raggiungibile | `get_heu_health` |

Due famiglie di oggetti:
- **Documenti nativi HEU** — creati con l'editor in-app della piattaforma (ID a forma di UUID, es. `5135e7b2-196b-...`).
- **PDF caricati** — file PDF con firmatari e campi firma posizionati sopra (ID numerici, es. `68`).

---

## Requisiti

- Python ≥ 3.10
- API key HEU Legal — nella UI: **Profile → API Keys → Generate API Key** (richiede subscription **Enterprise**; massimo 2 chiavi attive)
- Per i flussi da template: almeno un template creato sulla piattaforma (oppure crealo via API con `create_pdf_template`)

## Installazione

**Da PyPI:**

```bash
pip install heu-mcp
```

**Da sorgenti:**

```bash
git clone https://github.com/heulegal/heu-mcp.git
cd heu-mcp
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configurazione

### Claude Desktop

Modifica `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) o `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "heu": {
      "command": "heu-mcp",
      "args": [],
      "env": {
        "HEU_API_KEY": "la_tua_api_key_qui"
      }
    }
  }
}
```

Se installato da sorgenti con venv:

```json
{
  "mcpServers": {
    "heu": {
      "command": "/path/assoluto/heu-mcp/venv/bin/python",
      "args": ["/path/assoluto/heu-mcp/server.py"],
      "env": {
        "HEU_API_KEY": "la_tua_api_key_qui"
      }
    }
  }
}
```

Riavvia Claude Desktop dopo la modifica.

### Claude Code (CLI)

```bash
claude mcp add heu heu-mcp -e HEU_API_KEY=la_tua_api_key_qui
```

---

## Server remoto (multi-utente)

Oltre alla modalità locale (stdio) descritta sopra, il progetto include un **server remoto** che espone i tool via HTTPS, così gli utenti si collegano senza installare nulla: inseriscono la propria API key HEU una volta, in un flusso OAuth.

**Caratteristiche:**
- Transport **Streamable HTTP**, autenticazione **OAuth 2.1** con PKCE S256, Dynamic Client Registration e Client ID Metadata Document
- **Multi-utente**: ogni connessione usa la API key del proprio utente, conservata cifrata (Fernet) e mai in chiaro nel database
- Refresh token con **rotazione** e revoca automatica della sessione in caso di riuso sospetto
- I/O di rete asincrono e parsing PDF su threadpool: una richiesta lenta non blocca gli altri utenti
- **Nessun accesso al disco del server per conto degli utenti**: i tool che leggono o scrivono percorsi locali (download su file, creazione da file, `locate_pdf_text` con `file_path`) non sono esposti in remoto. Le operazioni in blocco passano da link personali di caricamento e download (vedi sotto).

| Modalità | Tool disponibili |
|---|---|
| Locale (stdio) | 30: tutti, con i bulk che leggono/scrivono file sul computer dell'utente |
| Remoto | 25: tutti tranne i 7 che usano il disco, più `get_bulk_upload_link` e `get_bulk_job_status` |

### Avvio locale

```bash
pip install -e ".[remote]"
export HEU_MCP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export PUBLIC_URL="http://localhost:8080"
export HEU_MCP_DB="./heu-mcp.db"
heu-mcp-remote
```

### Deploy su Fly.io

```bash
fly launch --no-deploy
fly volumes create heu_data --size 1
fly storage create   # bucket Tigris a consumo per i file delle operazioni in blocco
fly secrets set HEU_MCP_SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
fly secrets set PUBLIC_URL="https://<nome-app>.fly.dev"
fly deploy
```

### Variabili d'ambiente del server remoto

| Variabile | Descrizione | Default |
|---|---|---|
| `HEU_MCP_SECRET_KEY` | Chiave Fernet per cifrare le API key degli utenti (**richiesta**) | — |
| `PUBLIC_URL` | URL pubblico del servizio, senza slash finale (**richiesta in produzione**) | `http://localhost:8080` |
| `MCP_PATH` | Path dell'endpoint MCP | `/mcp` |
| `HEU_MCP_DB` | Percorso del database SQLite | `/data/heu-mcp.db` |
| `ACCESS_TOKEN_TTL` / `REFRESH_TOKEN_TTL` | Durata token in secondi | 1 ora / 60 giorni |
| `BUCKET_NAME` + `AWS_*` | Storage S3-compatibile (Tigris) per caricamenti e risultati bulk; impostate da `fly storage create`. Senza, i file vanno in `<cartella del DB>/bulk` | — |
| `HEU_BULK_RETENTION` | Secondi dopo cui caricamenti, lavori e file vengono cancellati | `86400` (24 ore) |
| `HEU_BULK_MIN_INTERVAL_REMOTE` | Secondi minimi tra due chiamate all'API nei lavori in blocco | `1.2` |

### Endpoint esposti

| Endpoint | Scopo |
|---|---|
| `/mcp` | Endpoint MCP (richiede bearer token) |
| `/.well-known/oauth-protected-resource` | Metadati risorsa protetta (RFC 9728) |
| `/.well-known/oauth-authorization-server` | Metadati authorization server (RFC 8414) |
| `/register` · `/authorize` · `/token` | Flusso OAuth 2.1 |
| `/bulk/upload/<token>` | Pagina personale e temporanea per caricare il file di un invio in blocco |
| `/bulk/files/<token>` | Download del file di esito o dell'archivio ZIP di un lavoro (scade con il lavoro) |
| `/healthz` | Liveness probe |

### Variabili d'ambiente

| Variabile | Descrizione | Default |
|-----------|-------------|---------|
| `HEU_API_KEY` | API key HEU Legal (**richiesta**) | — |
| `HEU_BASE_URL` | URL base dell'API | `https://api.heulegal.com/v1` |
| `HEU_DOWNLOAD_DIR` | Cartella dove salvare i file scaricati | `/tmp` |
| `HEU_BULK_TIME_BUDGET` | Secondi massimi di lavoro per singola chiamata dei tool bulk in locale (poi risponde 'parziale') | `240` |
| `HEU_BULK_MIN_INTERVAL` | Secondi minimi tra due chiamate all'API nei tool bulk in locale | `1.1` |

---

## Operazioni in blocco

Quando i documenti da inviare o scaricare sono decine o centinaia, chiamare un tool per ognuno riempie la conversazione e consuma crediti a ogni passaggio. I tool bulk fanno tutto il lavoro in **una sola operazione**, e i dati delle righe non passano mai dalla conversazione: Claude riceve solo conteggi, gli errori principali e il link o il percorso del risultato.

### Invio in blocco: `create_pdf_documents_bulk`

Un file `.csv` o `.xlsx` con **una riga per documento**. Le colonne vengono collegate al template automaticamente, ignorando maiuscole e accenti:

| Nel template | Colonne riconosciute |
|---|---|
| Un solo firmatario | `Nome` (oppure `Nome` + `Cognome`), `Email` |
| Più firmatari, es. "Cliente" e "Fornitore" | `Nome Cliente` / `Cliente`, `Email Cliente`, `Nome Fornitore`, ... |
| Campi di testo | colonna con lo stesso nome dell'etichetta del campo (es. `Indirizzo`) |
| Nome del documento (facoltativo) | `Nome documento`; altrimenti `<nome template> - <primo firmatario>` |

La mappatura si può correggere con `column_mapping`, per esempio:

```json
{"firmatari": {"Cliente": {"nome": ["Nome", "Cognome"], "email": "Posta"}},
 "campi": {"Indirizzo": "Via"},
 "nome_documento": "Contratto {Ragione sociale}"}
```

**Sempre in due passi.** Con `dry_run=true` (default) il tool verifica tutte le righe senza inviare nulla (email, campi obbligatori del template, limiti dell'API, duplicati) e mostra la mappatura usata. Solo dopo la conferma dell'utente si invia con `dry_run=false`.

**Durante l'invio:** circa una richiesta al secondo, per restare nel limite dell'API; sui `429` si rispetta `Retry-After`; errori di rete e `5xx` vengono ritentati. Una riga con errori non ferma le altre. L'invio si interrompe da solo solo per problemi che colpirebbero tutte le righe (API key revocata, crediti FEA esauriti).

**File di esito:** le stesse righe del file originale più tre colonne: `document_id`, `stato` (`inviato`, `errore`, `non_valido`, `da_verificare`, `in_attesa`) ed `errore`. È anche il modo di riprendere: rilanciando sullo stesso file, o caricando il file di esito, le righe già inviate vengono saltate. Una riga che risultava "in invio" al momento di un'interruzione non viene mai reinviata alla cieca: se esiste un solo documento con quel nome viene adottato, altrimenti la riga resta `da_verificare`. Lo stesso succede a una riga già inviata e poi modificata nel file: non viene reinviata in automatico. Cambiare la mappatura fra un tentativo e l'altro non fa ripartire le righe già inviate. Per reinviare una riga dopo aver controllato, basta svuotare la sua cella `stato`.

**Nel connettore remoto:**
1. `get_bulk_upload_link` crea un link personale, valido 30 minuti, dove l'utente carica il file.
2. `create_pdf_documents_bulk` con `upload_id`: prima la verifica, poi, con `dry_run=false`, l'invio parte in background sul server.
3. `get_bulk_job_status` mostra l'avanzamento e, a fine lavoro, il link al file di esito.

I lavori vengono eseguiti **uno alla volta**: il limite dell'API HEU si conta per indirizzo IP e tutti gli utenti del server remoto escono dallo stesso IP. Confermare due volte lo stesso caricamento non crea un secondo invio.

**In locale:** si passa `file_path`; il file di esito viene scritto accanto all'originale (`contratti.xlsx` → `contratti.esito.xlsx`). Ogni chiamata lavora per circa 4 minuti; se le righe non sono finite la risposta dice `parziale` e basta richiamare il tool con gli stessi parametri.

### Download in blocco: `download_pdf_bundles_bulk`

Seleziona i documenti con un elenco di `document_ids` oppure con filtri: `folder` (la cartella HEU, cioè il tag), `status` (default `signed`, `any` per tutti), `created_from` / `created_to`, `name_contains`. Con `dry_run=true` conta i documenti senza scaricarli.

Per ogni documento salva il **bundle originale** (PDF firmato, audit trail, artefatti FES) come `<nome documento> - <id>.zip`. Con `include_reading_copy=true` aggiunge una **copia unica in PDF** (documento seguito dall'audit trail). È solo una copia di consultazione: riscrivere il PDF invalida la firma digitale incorporata, quindi il valore legale resta nel bundle, che non viene mai modificato.

- **Remoto:** il server prepara un archivio ZIP in background (al massimo 1.000 documenti) e `get_bulk_job_status` restituisce il link per scaricarlo.
- **Locale:** i file vengono salvati in `destination_folder`; quelli già presenti vengono saltati, quindi rilanciare il tool continua da dove si era fermato.

### Conservazione dei dati (server remoto)

File caricati, righe dei lavori, file di esito e archivi restano sul server **per 24 ore** e poi vengono cancellati automaticamente. Le righe sono cifrate nel database e i file caricati sono cifrati prima di arrivare allo storage. Ogni caricamento, lavoro e link appartiene all'utente che l'ha creato: gli altri utenti non possono vederli né usarli. I link di download sono personali e scadono con il lavoro; chiunque abbia il link può scaricare il file finché è valido, quindi va trattato come un documento riservato.

### Risposte compatte

I tool che creano o modificano documenti (`create_pdf_document`, `create_heu_document`, `create_pdf_document_from_upload`, `create_pdf_template`, `update_pdf_template`) restituiscono un riepilogo: ID, nome, stato, tipo di firma e firmatari ridotti a ID ed email. Per i template, invece delle coordinate di ogni campo, il numero di campi per tipo. Con `verbose=true` restituiscono l'oggetto completo dell'API.

---

## Riferimento completo dei tool

### 🩺 Health

| Tool | Parametri | Cosa ritorna |
|---|---|---|
| `get_heu_health` | — | `{ message: "ok", status: 200 }` se l'API è operativa |

### 📄 Documenti nativi HEU

| Tool | Parametri | Cosa fa |
|---|---|---|
| `list_heu_documents` | `type` (document/template), `sort` (asc/desc), `created_from` + `created_to` (ISO 8601), `have_editors_signed` — tutti opzionali | Lista documenti/template con stato, membri, firme. ⚠️ Le due date vanno passate **sempre insieme**, altrimenti l'API può restituire risultati incompleti |
| `get_heu_document` | `document_id` | Dettaglio completo: nome, stato (`to_sign`/`in_progress`/`in_review`/`completed`/`signed`), owner, editors, members con `has_signed` e `signed_at`, tags |
| `list_heu_document_placeholders` | `document_id` | Elenco delle chiavi placeholder sostituibili nel testo del template |
| `create_heu_document` ✋ | `source_document_id`, `email_subject`, `email_text`, `email_to` (lista), `document_name`, `document_type`, `placeholders` (mappa chiave→valore) | Crea un documento da un template, sostituisce i placeholder e lo condivide via email ai destinatari |
| `prompt_heu_document_signature` ✋ | `document_id` | Invia il sollecito di firma. Limite: 1 ogni 24h per documento (429 con `Retry-After` se superato) |
| `read_heu_document` | `document_id`, `pages` (es. `"1-3"`, `"5"`, `"1,3,5-7"`), `layout`, `has_index`, `has_footer` | **Estrae il testo** del documento e lo restituisce in conversazione, senza salvare nulla su disco. Max 100 pagine se `pages` è omesso |
| `extract_heu_document_parties` | `document_id`, `pages`, `include_text` | **Dati delle parti**: combina i firmatari registrati con l'estrazione dal testo di codici fiscali, P.IVA, codice univoco SDI, email, PEC, luogo+data di nascita, indirizzi, CAP. Pattern ottimizzati per contratti italiani |
| `download_heu_document_pdf` | `document_id`, `layout` (codici UI: 100, 200-204, 210-214, 220-224, 230-234), `has_index`, `has_footer`, `output_path` | Genera e salva il PDF su disco; ritorna il path |

### 📎 PDF caricati

| Tool | Parametri | Cosa fa |
|---|---|---|
| `list_pdf_documents` | `type` (**richiesto**: document/template), `sort` | Lista PDF con stato (`to_sign`/`in_progress`/`signed`), tipo firma (FES/FEA), firmatari |
| `get_pdf_document` | `document_id` | Dettaglio: nome, stato, `signature_type`, date, firmatari con `has_read`/`has_signed` |
| `list_pdf_document_signers` | `document_id` | Firmatari del PDF: id, nome, email, ha letto, ha firmato |
| `list_pdf_document_signer_placeholders` | `document_id`, `signer_id` | Campi (firma/testo/checkbox) assegnati a un firmatario specifico, con posizione e stato di compilazione |
| `list_pdf_document_placeholders` | `document_id` | Tutti i campi del PDF |
| `create_pdf_document` ✋ | `source_document_id`, `email_subject`, `email_body`, `signers` (con `source_id`, `full_name`, `email`), `document_name`, `signature_type` (fes/fea), `placeholders` precompilabili | Crea un PDF firmabile **da un template esistente** e invia gli inviti. Con `fea` servono crediti sufficienti (422 altrimenti) |
| `prompt_pdf_document_signature` ✋ | `document_id` | Sollecito di firma per il PDF |
| `read_pdf_document` | `document_id`, `pages` | Estrae il testo del PDF (incluso quello **firmato**) e lo restituisce in conversazione |
| `extract_pdf_document_parties` | `document_id`, `pages`, `include_text` | Dati delle parti (come sopra) per i PDF caricati |
| `download_pdf_document` | `document_id`, `output_path` | Scarica il PDF — **versione firmata se disponibile** — e ritorna il path |
| `download_pdf_audit_trail` | `document_id`, `output_path` | Scarica l'**audit trail**: il registro PDF di chi ha letto/firmato e quando |
| `download_pdf_bundle` | `document_id`, `output_path` | Scarica lo **ZIP del fascicolo legale**: PDF firmato + audit trail + artefatti FES. Ideale per archiviazione a valore probatorio |
| `cancel_pdf_document` ✋ | `document_id` | **Annulla una richiesta di firma inviata**: il documento sparisce dagli elenchi e i link di firma vengono invalidati. Rifiutato con 409 se qualcuno ha già firmato |

### 📦 Operazioni in blocco

| Tool | Parametri | Cosa fa |
|---|---|---|
| `create_pdf_documents_bulk` ✋ | `source_document_id`, `email_subject`, `email_body`, `file_path` (locale) **oppure** `upload_id` (remoto), `signature_type`, `column_mapping`, `dry_run` (default `true`) | Invia lo stesso template a una riga per destinatario di un CSV/XLSX: verifica, invio con ripresa sicura e file di esito. Vedi [Operazioni in blocco](#operazioni-in-blocco) |
| `download_pdf_bundles_bulk` | `document_ids` oppure `folder`, `status`, `created_from`, `created_to`, `name_contains`; `include_reading_copy`, `dry_run`, `destination_folder` (solo locale) | Scarica i bundle firmati di molti documenti: in una cartella del computer (locale) o in un archivio ZIP (remoto) |
| `get_bulk_upload_link` | — | *Solo remoto.* Link personale e temporaneo per caricare il file di un invio in blocco |
| `get_bulk_job_status` | `job_id` | *Solo remoto.* Avanzamento di un lavoro in blocco e, a fine lavoro, il link al file di esito o all'archivio |

I tool che creano o modificano documenti accettano anche `verbose` (default `false`): vedi [Risposte compatte](#risposte-compatte).

### 🧩 Template PDF (gestione via API)

| Tool | Parametri | Cosa fa |
|---|---|---|
| `create_pdf_template` ✋ | `file_path` (PDF locale ≤ 5 MB), `document_name`, `signers` (`source_id`, `full_name`), `placeholders` (tipo, posizione %, pagina) | Crea un **template riutilizzabile** caricando un PDF dal computer. L'ID restituito si usa come `source_document_id` in `create_pdf_document` |
| `create_pdf_document_from_upload` ✋ | `file_path`, `document_name`, `email_subject`, `email_body`, `signers` (con email), `placeholders`, `signature_type` | **Scorciatoia completa**: carica un PDF e lo manda subito in firma, senza creare prima il template. Il documento nasce `to_sign` e i firmatari ricevono l'email immediatamente |
| `locate_pdf_text` | `file_path` **oppure** `document_id`, `search_terms` (default: parole chiave firma), `pages`, `include_all_lines` | **Trova le coordinate di testi nel PDF** (in %, origine in basso a sinistra — lo stesso sistema dei placeholder). È il tool che permette all'AI di posizionare i campi da sola: cerca "Firma", i nomi delle parti o qualsiasi ancora, e ottiene pagina + posizione di ognuna |
| `preview_pdf_template` | `document_id`, `output_path` | Scarica un'**anteprima annotata**: ogni campo è disegnato come riquadro etichettato con tipo e firmatario. Per verificare le posizioni prima dell'invio |
| `update_pdf_template` ✋ | `document_id`, `signers` (set completo sostitutivo), `placeholders` (idem; `[]` li cancella tutti), `document_name` | Sostituzione integrale di firmatari e campi di un template (l'ID resta invariato). Solo il proprietario |
| `delete_pdf_template` ✋ | `document_id` | Elimina (nasconde) il template da tutti gli elenchi |

✋ = il tool ha effetti verso l'esterno (crea, invia email, elimina): Claude chiede sempre conferma prima di eseguirlo.

#### Come si posizionano i placeholder

- `position_x` / `position_y`: **percentuale** della pagina (0–100 esclusi), origine **in basso a sinistra** (come nell'editor dell'app).
- `page_number`: parte da 1.
- Tipi: `signature`, `initials`, `text` (richiede `text_label`), `checkbox_optional`, `checkbox_required`.
- I firmatari si collegano ai campi tramite `source_id` → `signer_source_id`.
- I PDF ruotati (`/Rotate 90/180/270`) vengono rifiutati dall'API.

---

## Flussi di lavoro tipici

### 0. Invio in firma "intelligente": l'AI mappa i campi da sola

> *"Prendi `/Users/me/Desktop/Contratto.pdf`, trova dove devono firmare le parti e mandalo a cliente@example.com e fornitore@example.com."*

Cosa succede dietro le quinte:

1. **`locate_pdf_text`** analizza il PDF e trova le ancore: le righe "Firma del Cliente" / "Firma del Fornitore" (o i nomi delle parti) con le loro coordinate esatte in percentuale.
2. Claude **propone la mappatura**: *"Metto il campo firma del cliente a pagina 4, sopra l'etichetta 'Firma del Cliente' (x 17%, y 14%), e quello del fornitore accanto (x 50%, y 14%). Confermi?"*
3. Alla conferma, **`create_pdf_document_from_upload`** carica il PDF con i placeholder posizionati e invia le email di firma.
4. (Opzionale) **`preview_pdf_template`** per un controllo visivo se si è passati da un template.

Le coordinate restituite da `locate_pdf_text` sono già nel sistema dei placeholder HEU (percentuale, origine in basso a sinistra): nessuna conversione necessaria. Per layout complessi si può chiedere l'intera mappa della pagina con `include_all_lines=true`, o cercare termini specifici (`search_terms=["Il Committente", "Il Prestatore"]`).

⚠️ Limite: funziona sui PDF con testo. Le scansioni senza OCR non hanno testo estraibile — in quel caso indicare le posizioni manualmente.

### 1. Mandare in firma un PDF dal computer (tutto via chat)

> *"Prendi `/Users/me/Desktop/NDA.pdf` e mandalo in firma a Mario Rossi (mario@example.com). Campo firma in basso a destra dell'ultima pagina, oggetto email 'NDA da firmare'."*

Claude usa `create_pdf_document_from_upload` → il documento è creato e Mario riceve subito l'email. Poi:

> *"Mario ha firmato?"* → `get_pdf_document`
> *"Sollecitalo"* → `prompt_pdf_document_signature`
> *"È firmato, scaricami il fascicolo completo"* → `download_pdf_bundle`

### 2. Contratti ricorrenti con template

> *"Crea un template dal file `Contratto-tipo.pdf` con due firmatari: cliente e fornitore. Firma del cliente a pagina 3 in basso."* → `create_pdf_template`
> *"Fammi vedere l'anteprima per controllare le posizioni"* → `preview_pdf_template`
> *"Ora usalo per mandare il contratto ad ACME srl"* → `create_pdf_document`

### 3. Analisi documentale (l'AI legge i contratti)

> *"Riassumi il contratto `abc-123` e dimmi durata e condizioni di recesso"* → `read_heu_document`
> *"Confronta le clausole di responsabilità dei contratti X e Y"* → due `read_heu_document`
> *"Estrai i dati delle parti: ragione sociale, P.IVA, SDI, PEC"* → `extract_heu_document_parties`

L'estrazione parti è pensata per l'integrazione con flussi di **fatturazione elettronica italiana**: il codice univoco SDI e la P.IVA estratti dal contratto possono alimentare direttamente l'anagrafica del gestionale.

### 4. Monitoraggio e amministrazione

> *"Quali documenti di luglio non sono ancora stati firmati da tutti?"* → `list_heu_documents` con date + `have_editors_signed=false`
> *"Annulla la richiesta di firma del PDF 42, l'abbiamo mandata alla persona sbagliata"* → `cancel_pdf_document`

---

## Comportamenti e limiti da conoscere

| Cosa | Limite / comportamento |
|---|---|
| Rate limit API | **300 richieste / 5 minuti** (header `X-RateLimit-*` nelle risposte; 429 con `Retry-After` oltre soglia) |
| Solleciti firma | 1 ogni **24 ore** per documento |
| Upload PDF | Max **5 MB**, `application/pdf`, non ruotati |
| Lettura testo | Max **100 pagine** se `pages` non è specificato (il payload segnala `truncated: true`); PDF scansionati senza OCR non hanno testo estraibile |
| Firma FEA | Richiede **crediti FEA** disponibili per ogni firmatario (422 se insufficienti) |
| `list_heu_documents` con date | Passare **sempre entrambe** `created_from` e `created_to` |
| Annullamento PDF | Possibile **solo senza attività di firma** (409 altrimenti) |
| Download | I binari vengono salvati su disco (`HEU_DOWNLOAD_DIR`), mai trasmessi nel canale MCP |
| Invio in blocco | Massimo **5.000 righe** per file e **10 MB** per il caricamento remoto; massimo 10 firmatari per documento |
| Download in blocco remoto | Massimo **1.000 documenti** per archivio |
| File dei lavori remoti | Disponibili **24 ore**, poi cancellati |

## Stati dei documenti

| Stato | Significato |
|---|---|
| `to_sign` | In attesa di firme |
| `in_progress` | In preparazione/modifica |
| `in_review` | In revisione |
| `completed` | Flusso completato (non firmato) |
| `signed` | Completamente firmato |

## Sicurezza

- L'API key è letta **solo** da variabile d'ambiente: mai nel codice, mai nelle risposte, mai nei log.
- I tool con effetti esterni (✋) sono istruiti per richiedere sempre conferma esplicita all'utente.
- I file scaricati restano sul filesystem locale. Nel server remoto nessun tool legge o scrive percorsi del disco del server.

## Sviluppo

```bash
git clone https://github.com/heulegal/heu-mcp.git
cd heu-mcp
python3 -m venv venv
source venv/bin/activate
pip install -e ".[remote]" -r requirements-dev.txt

# Test (le chiamate all'API HEU sono simulate)
python -m pytest tests

# Avvio manuale per debug
HEU_API_KEY=... python server.py
```

La spec OpenAPI di riferimento è pubblicata su `https://api.heulegal.com/v1/specs/v1.yaml` (docs interattive: `https://api.heulegal.com/v1/docs`).

## Licenza

MIT — vedi [LICENSE](LICENSE).

## Link

- [HEU Legal](https://heulegal.com)
- [Documentazione API HEU](https://api.heulegal.com/v1/docs)
- [Model Context Protocol](https://modelcontextprotocol.io)
- [Pacchetto PyPI](https://pypi.org/project/heu-mcp/)
