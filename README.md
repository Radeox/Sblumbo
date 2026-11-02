# Obsidian Daily Worker 🧠🗂️

Un worker automatico e zero-dependency in Python che legge le **Daily Note** del tuo Vault Obsidian, ne estrae e rielabora i contenuti tramite un modello LLM locale (**Ollama**) e li smista ordinatamente nelle rispettive cartelle e file del Vault, effettuando infine un commit Git automatico.

---

## ✨ Cosa fa

A partire da una Daily Note strutturata in 4 sezioni (`Todo`, `Ideas`, `Notes`, `Links`):

1. **📝 Todo** → Estrae tutte le azioni atomiche (separando eventuali frasi con più task) e le accoda al file `Tasks.md` nel formato `- [ ] Azione #YYYY-MM-DD`.
2. **💡 Ideas** → Separa ogni idea dell'elenco puntato, genera titolo sintetico, tag YAML e corpo riformulato con wikilink, creando un file `.md` dedicato nella cartella `Ideas/`.
3. **📦 Notes** → Separa ogni appunto, genera un titolo fedele al testo, tag pertinenti e corpo Markdown senza inventare dettagli, salvando ogni nota in `Notes/`.
4. **🔗 Links** → Estrae gli URL (anche con testo di accompagnamento o sottotitoli di raggruppamento), scarica `<title>` e `<meta description>` della pagina web, li traduce/riassume in italiano tramite l'LLM e crea una scheda in `Links/`.
5. **🧹 Pulizia & Git Commit** → Se e solo se tutte le sezioni vengono elaborate senza errori, rimuove la Daily Note processata ed esegue un commit Git nel Vault con il riepilogo degli elementi estratti (es. `Ingest 2026-11-02 ore 19:30:00 — 3 todo, 1 idea, 2 note, 4 links`).

---

## 📁 Struttura del Progetto

```text
obsidian_worker/
├── obsidian_worker.py      # Entry-point CLI e orchestrazione fail-safe
├── .env                    # Configurazione locale (ignorata da Git)
├── .env.example            # Template di configurazione
├── .gitignore              # Esclusioni Git
├── README.md               # Documentazione del progetto
└── worker/                 # Pacchetto sottomoduli
    ├── __init__.py         # Esportazioni pubbliche del pacchetto
    ├── config.py           # Loader .env zero-dependency e dataclass Config
    ├── parser.py           # Parser deterministico sezioni Daily Note ed elenchi
    ├── prompts.py          # System prompt anti-allucinazione e JSON Schema
    ├── ollama_client.py    # Client HTTP per Ollama e health-check preventivo
    ├── scraper.py          # Parser HTML per metadati (<title>, OpenGraph, meta description)
    ├── processors.py       # Logica di trasformazione, anti-collisione file e sanitizzazione YAML
    └── git_utils.py        # Validazione Vault, risoluzione note e commit Git sicuro
```

---

## ⚙️ Configurazione (`.env`)

Copia il file `.env.example` in `.env` e personalizza le variabili secondo il tuo ambiente:

```bash
cp .env.example .env
```

| Variabile | Default | Descrizione |
| :--- | :--- | :--- |
| `VAULT_PATH` | `~/Obsidian` | Percorso assoluto o con `~` della radice del tuo Vault Obsidian. |
| `TODO_FILE` | `Tasks.md` | File di destinazione per i task (relativo a `VAULT_PATH` o assoluto). |
| `NOTES_DIR` | `Notes` | Cartella di destinazione per le note (relativa a `VAULT_PATH` o assoluta). |
| `IDEAS_DIR` | `Ideas` | Cartella di destinazione per le idee (relativa a `VAULT_PATH` o assoluta). |
| `LINKS_DIR` | `Links` | Cartella di destinazione per i link (relativa a `VAULT_PATH` o assoluta). |
| `DAILY_DIR` | `Daily` | Cartella in cui risiedono le Daily Note giornaliere. |
| `OLLAMA_URL` | `http://127.0.0.1:11434/api/chat` | Endpoint API chat di Ollama. |
| `OLLAMA_MODEL` | `qwen2.5:3b` | Nome del modello Ollama da utilizzare. |
| `OLLAMA_TIMEOUT` | `300` | Timeout massimo in secondi per ogni richiesta a Ollama. |
| `OLLAMA_TEMPERATURE` | `0.1` | Temperatura del modello (valori bassi riducono le allucinazioni). |
| `OLLAMA_NUM_PREDICT` | `512` | Numero massimo di token generabili per singola chiamata. |
| `URL_FETCH_TIMEOUT` | `6` | Timeout in secondi per lo scraping dei metadati di ogni URL. |
| `GIT_AUTO_COMMIT` | `true` | Se `true`, esegue automaticamente il commit Git nel Vault a fine ingest. |

---

## 🚀 Utilizzo

Lo script non richiede librerie Python esterne (usa esclusivamente la standard library di Python 3.10+). Assicurati solo che `ollama` sia attivo e che il modello configurato sia scaricato (`ollama pull qwen2.5:3b`).

### 1. Esecuzione con percorso esplicito
```bash
python3 obsidian_worker.py ~/Obsidian/Daily/2026-11-02.md
```

### 2. Esecuzione rapida (solo data o nota di oggi)
Se ometti l'argomento, il worker cercherà automaticamente la nota di oggi (`YYYY-MM-DD.md`) dentro `DAILY_DIR`:
```bash
# Cerca automaticamente ~/Obsidian/Daily/<OGGI>.md
python3 obsidian_worker.py

# Oppure passa solo la data:
python3 obsidian_worker.py 2026-11-02
```

### 3. Opzioni CLI e modalità di test (`--dry-run`)
Puoi verificare come vengono lette e suddivise le sezioni della tua Daily Note **senza** interrogare Ollama e **senza** toccare alcun file:

```bash
python3 obsidian_worker.py 2026-11-02 --dry-run
```

Altre flag utili:
- `--keep-daily`: elabora la nota ma **non** la elimina al termine.
- `--no-commit`: elabora la nota ma salta il commit Git nel Vault.
- `--env-file /percorso/custom.env`: usa un file `.env` alternativo.

---

## 📝 Formato Atteso della Daily Note

Il parser riconosce le 4 sezioni indipendentemente dalla presenza di spazi dopo l'emoji o dal livello dell'heading (`#`, `##`, `###`, `####`):

```markdown
### 📝 Todo
* Spostare le note da Keep e disinstallare l'app
* Configurare il backup automatico

### 💡 Ideas
* Creare una dashboard per monitorare i consumi di casa

### 📦 Notes
* Riunione di 1 ora sul progetto Serf
* Comando rapido per controllare le porte aperte con ss -tulpn

### 🔗 Links
* https://github.com/raspberrypi/rpi-image-gen
* serfsrl.it - Sito ufficiale filtri aria
```

---

## 🛡️ Fail-Safe e Sicurezza Dati

Il worker integra protezioni specifiche per evitare perdite di dati o allucinazioni del modello:

1. **Zero Allucinazioni su Categorie Vuote**:
   - Il parser line-by-line isola rigorosamente ogni sezione. Una sezione vuota (es. `### 📝 Todo` seguito subito da `### 💡 Ideas`) o contenente solo marcatori di elenco vuoti (`* `, `- [ ]`) viene scartata a monte senza interrogare l'LLM.
   - Evita il bug per cui `qwen2.5:3b` ripeteva le istruzioni del prompt di sistema come se fossero task quando riceveva input vuoti o mal catturati.
2. **Isolamento tra Link, Note e Idee**:
   - Eventuali sottotitoli usati per raggruppare i link (es. `### Herolapse` dentro `### 🔗 Links`) non interrompono più la sezione `Links` e non vengono scambiati per URL.
   - Se un URL puro finisce per errore in `Notes` o `Ideas`, viene filtrato in modo che il modello non inventi note o idee fittizie a partire da un semplice indirizzo web.
3. **Controllo Preventivo del Vault e della Nota**:
   - Verifica che `VAULT_PATH` esista, sia una directory valida e abbia i permessi di lettura/scrittura.
   - Impedisce di processare ed eliminare accidentalmente il file indice `Daily/Daily.md`.
4. **Health-Check Preventivo di Ollama**:
   - Prima di iniziare qualsiasi operazione, verifica che il server Ollama risponda e che il modello scelto sia installato.
5. **Rimozione Transazionale della Daily Note**:
   - Se anche una sola sezione va in errore (es. crash di Ollama durante l'elaborazione), la Daily Note originale **non viene eliminata**, preservando tutti i tuoi appunti.
6. **Anti-Collisione File e Sanitizzazione YAML**:
   - Se due note generano lo stesso titolo/slug, non si sovrascrivono: viene generato un suffisso progressivo (`Titolo_2.md`).
   - I titoli con doppi apici o due punti (`:`) e i tag con spazi vengono sanitizzati per garantire un frontmatter YAML sempre valido per Obsidian.
   - Assicura che `.stversions` e `.stfolder` (Syncthing) siano ignorati da Git prima del commit.
