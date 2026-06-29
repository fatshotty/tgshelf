# tgshelf

Cloud storage su Telegram con un vero filesystem virtuale.

`tgshelf` salva i payload dei file nei canali Telegram e mantiene i metadati del
filesystem in PostgreSQL. Espone cartelle e file tramite CLI Python, API aiohttp,
endpoint HTTP per download, WebDAV/rclone e una Web UI React.


## Cosa offre

- Metadati in PostgreSQL per un albero virtuale con node id stabili da 10
  caratteri.
- Canali Telegram come layer di storage fisico, con ereditarietà opzionale del
  canale per cartella.
- Storage inline nel DB per file piccoli e storage multipart su Telegram per file
  più grandi.
- Download HTTP `/download/{file_id}` con supporto Range, parità HEAD/GET,
  ETag, 304 e 416.
- Download parallelo tramite più bot o account utente, con failover,
  cooldown e fallback opzionale sugli utenti.
- Operazioni filesystem: creazione cartelle, rename, move, copy, soft delete,
  restore, purge, search, size ricorsiva, merge parts, split parts e reorder
  parts.
- Workflow CLI per account/sessioni, operazioni filesystem, sync, download,
  generazione `.strm`, controllo bot.
- Web UI per browse, ricerca, metriche, gestione tree, editing inline di testi e
  gestione delle parti dei file Telegram-backed.
- Endpoint WebDAV per rclone, più invalidazione opzionale della cache rc di
  rclone tramite changes feed PostgreSQL.
- Watcher live opzionale per importare file pubblicati nel canale master mentre
  il server è in esecuzione.
- Osservabilità tramite `/status`, `/metrics`, `/metrics.txt`, metriche SSE per
  Web UI, log strutturati e notifiche Telegram opzionali.

## Stack

- Python 3.12+
- Telethon
- PostgreSQL
- SQLAlchemy async + Alembic
- aiohttp
- Vite + React

## Note sulle performance

Il throughput Telegram dipende da tipo di account, datacenter, rete del server e
limiti lato Telegram. Nei test locali, il download parallelo può aggregare più
bot/account per aumentare il throughput effettivo finché il deployment non
raggiunge i limiti di Telegram o della rete.

## Setup di sviluppo

Crea una virtualenv e installa il pacchetto Python in modalità editable:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Crea e modifica la configurazione runtime:

```sh
cp config.example.yaml config.yaml
```

Prepara il database:

```sh
alembic upgrade head
```

Compila la Web UI quando vuoi servire gli asset statici dall'app Python:

```sh
cd webui
npm install
npm run build
```

Avvia il server:

```sh
tgshelf --config config.yaml serve
```

## Verifica

Python:

```sh
python -m pytest -q
```

Web UI:

```sh
cd webui
npm run typecheck
npm run build
```

## Configurazione

```yaml
# Configurazione di esempio: tutti i valori sensibili sono dummy.
# Non usare api_id, api_hash, bot_token o channel così come sono.

data: ./data            # directory locale per sessioni file e stato runtime

# DSN PostgreSQL. La variabile d'ambiente DB sovrascrive questo valore.
db: postgresql+asyncpg://DB_USER:DB_PASS@DB_HOST:DB_PORT/DB_NAME

logger: info            # no | error | warn | info | debug

# Dove salvare le sessioni Telegram:
#   db   = tabella tg_sessions, consigliato per istanze singole
#   file = {data}/{name}.session, utile quando ogni istanza ha sessioni proprie
session_storage: db

# Numero di connessioni TCP per client Telegram sul data path.
# 0 o 1 = comportamento standard; 2 = valore prudente; sopra 3 è sconsigliato.
concurrent_tcp_connections: 1

telegram:
  users:                # account utente e bot; i bot hanno bot_token
    - name: main
      api_id: 123456    # dummy: sostituisci con il tuo api_id Telegram
      api_hash: "0123456789abcdef0123456789abcdef"  # dummy
    - name: bot01
      api_id: 123456    # dummy
      api_hash: "0123456789abcdef0123456789abcdef"  # dummy
      bot_token: "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # dummy

  upload:
    # I file <= min_size byte restano inline nel DB, non su Telegram.
    # Deve essere un multiplo di 524288, la dimensione parte Telegram.
    min_size: 2097152
    # Canale master mappato alla root "/". Dummy: sostituisci con il tuo -100...
    channel: -1001234567890

  # Watcher opzionale: bot dedicato, diverso dai bot in telegram.users.
  # Deve essere admin del canale master. Importa solo i file pubblicati mentre
  # `serve` è in esecuzione.
  main_bot:
    api_id: 123456      # dummy
    api_hash: "0123456789abcdef0123456789abcdef"  # dummy
    bot_token: "987654321:AAyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"  # dummy

  notify:
    # Bot HTTP API opzionale per notifiche CRITICAL/ERROR.
    # Vuoto = solo log locali.
    bot_token:          # opzionale, dummy se valorizzato
    # Destinazione opzionale: id numerico (-100.../-...) o @username.
    # Vuoto = canale master configurato sopra.
    channel:
    # Template opzionale delle notifiche. Ogni riga con placeholder senza valore
    # viene omessa, così puoi riordinare o togliere campi senza label vuote.
    template: |
      [tgshelf:{severity}] {title}

      Impact: {impact}
      Scope: {scope}
      File: {file_path}
      Node: {node_id}
      Part: {part_idx}
      Channel: {channel_id}
      Account: {account}
      Cause: {cause}
      Action: {action}
      Time: {time}
      Host: {host}
      Key: {key}
    warning_window: 300

  # Rate limit proattivo per account. calls=0 disabilita.
  # coordination=redis è una estensione prevista, non ancora implementata.
  rate_limit:
    calls: 0
    window: 1.0
    coordination: memory

download:
  multi_bot_download: 3         # bot paralleli per download; 1 = sequenziale
  allow_user_fallback: false    # usa account utente se il pool bot è esaurito
  chunk_timeout: 6              # secondi senza chunk prima di sostituire il bot
  # Soglia soft dei buffer stimati. 0 = disabilitata.
  memory_soft_limit: 0

operations:             # throttling per move/copy/delete massivi su Telegram
  concurrent: 3
  sleep: 1              # pausa in secondi tra batch
  batch: 10             # operazioni per batch

http:
  enabled: true
  host: 127.0.0.1
  port: 3000
  user: ""              # vuoto = niente basic auth
  pass: ""
  ignore_auth_for: []   # CIDR senza basic auth, es. ["192.168.1.0/24"]

strm:
  destination: ./strm-folder   # cartella locale dentro cui generare i file .strm
  source: /             # cartella virtuale da cui partire per generare i file .strm
  # Template arbitrario per il contenuto dei file .strm.
  # Il path deve iniziare con /download/{file_id}; il resto è decorativo.
  # Placeholder utili: {file_id}, {filename}, {channel_id}, {parts_dash},
  # {size}, {mime}.
  template: "http://127.0.0.1:3000/download/{file_id}/{filename}"
  clear_folder: false   # forza la pulizia dell'intera cartella locale prima di generare l'albero .strm

changes_feed:
  enabled: false        # trigger PostgreSQL + LISTEN/NOTIFY
  retention_days: 7

# Integrazione rclone: WebDAV data-plane e bridge rc per invalidare la cache.
rclone:
  webdav_enabled: false   # espone WebDAV read-write su /dav
  bridge_enabled: false   # LISTEN changes_feed -> vfs/forget
  # Segreto condiviso per registrare l'endpoint rc via header X-Tgshelf-Token.
  # Vuoto = self-registration disabilitata.
  register_token: "secret"
  # CIDR aggiuntivi ammessi per host rc dichiarati dai client rclone.
  allowed_rc_networks: []
  registry_ttl: 600       # secondi prima di rimuovere un mount inattivo
```

I valori obbligatori sono la DSN PostgreSQL e `telegram.upload.channel`. Serve
almeno un account utente per upload e operazioni di gestione. Gli account bot
sono opzionali, ma sono la parte che rende utile il download parallelo.

`telegram.main_bot` è un bot dedicato al watcher, non uno degli account in
`telegram.users`. Importa solo i file pubblicati nel canale master mentre
`serve` è in esecuzione.

La variabile d'ambiente `DB` sovrascrive la chiave `db`, utile per URL database
specifici del deployment.

## Esempi CLI

Tutti i comandi accettano `--config`; il default è `./config.yaml`.

```sh
# Ispeziona account configurati e sessioni salvate.
tgshelf --config config.yaml accounts list

# Login interattivo di un account utente definito in telegram.users.
tgshelf --config config.yaml accounts login main

# Registra un bot il cui bot_token è già presente in config.yaml.
tgshelf --config config.yaml accounts add-bot bot01

# Avvia API HTTP, Web UI, watcher, metriche e superfici WebDAV
# abilitate dalla configurazione.
tgshelf --config config.yaml serve

# Crea cartelle nel filesystem virtuale.
tgshelf --config config.yaml mkdir /folder/sub-folder

# Carica un albero locale nel filesystem virtuale.
tgshelf --config config.yaml sync ./folder-to-up --dest /folder/sub-folder [--concurrent 3] [--delete-source]

# Lista, misura, stampa, copia, sposta, soft-delete e purge dei nodi.
tgshelf --config config.yaml ls /folder
tgshelf --config config.yaml search readme
tgshelf --config config.yaml du -H /folder/sub-folder
tgshelf --config config.yaml cat /notes/readme.txt
tgshelf --config config.yaml cp /notes/readme.txt /archive
tgshelf --config config.yaml mv /archive/readme.txt /folder/sub-folder
tgshelf --config config.yaml rm /notes/readme.txt
tgshelf --config config.yaml purge /notes/readme.txt

# Scarica un file o una cartella. I file parziali esistenti vengono ripresi,
# salvo uso esplicito di --overwrite.
tgshelf --config config.yaml download /archive/big-file.bin --dest ./restore [--concurrent 4]

# Genera file .strm a partire dall'albero virtuale.
tgshelf --config config.yaml strm --source /folder --destination ./strm [--clear]

# Verifica o ripara la membership dei bot sui canali usati dal filesystem.
tgshelf --config config.yaml bots check
```

Esempio di remote WebDAV per rclone:

```sh
rclone config create tgshelf webdav \
  url http://127.0.0.1:3000/dav \
  vendor other

rclone mount tgshelf: /mnt/tgshelf
```
