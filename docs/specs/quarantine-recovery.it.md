# Recupero temporizzato della quarantena del pool

## Problema

Attualmente, tre timeout consecutivi durante la risoluzione o il recupero di un
chunk mettono un membro del pool in quarantena per l'intera durata del processo.
I membri in quarantena non hanno una scadenza di recupero e il metodo
`recover()` esistente non viene chiamato dal server in esecuzione. Il pool può
quindi rimanere esaurito fino al riavvio di tgshelf.

## Ambito

Lo stato di salute della quarantena è uno stato locale e transitorio del
processo. Non viene memorizzato in PostgreSQL. Ogni installazione di tgshelf
possiede un pool indipendente: Cloudboxes utilizza la famiglia di bot `c_*`,
mentre Hetzner utilizza la famiglia `h_*`. Lo stato di salute di un pool non deve
influenzare l'altro.

## Macchina a stati

Un membro parte dallo stato sano. Tre errori consecutivi di timeout lo spostano
nello stato `quarantined`, impostando la prima scadenza configurata. La sequenza
predefinita dei nuovi tentativi è di 5, 15, 30 e 60 minuti; l'ultima durata resta
il limite massimo per gli ulteriori tentativi falliti.

Quando scade un periodo di quarantena, una normale richiesta di download può
acquisire un membro nello stato `half_open`. Non vengono inviate richieste
Telegram in background o sintetiche. In ogni pool può esistere una sola probe
half-open alla volta.

- Un chunk recuperato correttamente riporta il membro allo stato sano e azzera
  lo stadio dei tentativi.
- Un timeout avanza lo stadio e avvia il periodo di quarantena successivo.
- Una probe annullata, soggetta a FloodWait o non idonea per il canale rimane in
  quarantena senza avanzare lo stadio. Potrà essere riprovata dopo l'eventuale
  cooldown locale applicabile.
- Gli errori provenienti da operazioni già in corso non fanno avanzare una
  quarantena aperta.

Le richieste non restano in attesa per periodi di quarantena della durata di
diversi minuti. Se non esiste un membro sano né una probe pronta, la richiesta termina
con una motivazione esplicita e lo stato mostra il tempo rimanente fino alla
prossima probe locale.

Le cronologie complete del recupero sono riportate nell'Appendice A.

## Configurazione

`download.quarantine_backoff_seconds` è un elenco non vuoto di durate positive
e non decrescenti. Il valore predefinito è:

```yaml
download:
  quarantine_backoff_seconds: [300, 900, 1800, 3600]
```

## Osservabilità

Lo stato del pool conserva i campi esistenti e aggiunge lo stato del membro, lo
stato half-open, il numero del tentativo di quarantena, il tempo di quarantena
rimanente e il conto alla rovescia del pool fino alla prossima probe. I log
utilizzano i marcatori `[quarantine]`, `[probe]` e `[recover]`.

## Compatibilità

La modifica non richiede migrazioni del database né cambiamenti alle sessioni
Telegram. Le configurazioni esistenti ereditano la sequenza predefinita. Il
cooldown FloodWait e l'idoneità per canale restano separati dalla quarantena
causata dai timeout.

## Appendice A - Cronologie operative

### A.1 - Sei bot sani e sei bot in quarantena

**Scenario:** pool di 12 bot, con `H1-H6` sani e `Q1-Q6` appena entrati in
quarantena. Si assume traffico continuo, successo di ogni probe al primo
tentativo e una durata indicativa di 3 secondi per probe.

| Tempo | Probe half-open | Bot disponibili | Bot in quarantena | Evento |
|---:|:---:|:---|:---|:---|
| `t+00:00` | - | `H1-H6` (6) | `Q1-Q6` (6) | Inizia l'attesa di 5 minuti |
| `t+05:00` | `Q1` | `H1-H6` (6) | `Q2-Q6` (5) | Parte la probe di `Q1` |
| `t+05:03` | `Q2` | `H1-H6`, `Q1` (7) | `Q3-Q6` (4) | `Q1` recuperato |
| `t+05:06` | `Q3` | `H1-H6`, `Q1-Q2` (8) | `Q4-Q6` (3) | `Q2` recuperato |
| `t+05:09` | `Q4` | `H1-H6`, `Q1-Q3` (9) | `Q5-Q6` (2) | `Q3` recuperato |
| `t+05:12` | `Q5` | `H1-H6`, `Q1-Q4` (10) | `Q6` (1) | `Q4` recuperato |
| `t+05:15` | `Q6` | `H1-H6`, `Q1-Q5` (11) | - | `Q5` recuperato |
| `t+05:18` | - | `H1-H6`, `Q1-Q6` (12) | - | Pool completamente sano |

I sei bot sani continuano a servire le richieste per tutta la sequenza. Poiché
è ammessa una sola probe half-open alla volta, il pool torna da 6 a 12 membri
sani circa 18 secondi dopo la prima scadenza.

### A.2 - Corsie temporali dei sei bot

**Legenda:** `Q` = quarantena, `P` = probe half-open, `H` = sano.

```text
Q1  QQQQQQQQQQQQQQQ  P--> HHHHHHHHHHH
Q2  QQQQQQQQQQQQQQQ     P--> HHHHHHHH
Q3  QQQQQQQQQQQQQQQ        P--> HHHHH
Q4  QQQQQQQQQQQQQQQ           P--> HHH
Q5  QQQQQQQQQQQQQQQ              P--> H
Q6  QQQQQQQQQQQQQQQ                 P--> H
    |                |                    |
  t+00:00          t+05:00              t+05:18

H1-H6  HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH
```

La probe viene concessa in ordine a un solo membro scaduto per volta. La riga
`H1-H6` mostra che i sei membri inizialmente sani restano operativi durante
l'intero recupero.

### A.3 - Probe che continuano a fallire

**Scenario:** un bot raggiunge tre timeout consecutivi e fallisce ogni probe.
La durata massima del backoff resta fissata a 60 minuti.

| Tempo schematico | Evento | Stato successivo | Prossima probe |
|---:|:---|:---|---:|
| `t+00m` | Terzo timeout consecutivo | Quarantena, tentativo 1 | `t+05m` |
| `t+05m` | Probe fallita | Quarantena 15m, tentativo 2 | `t+20m` |
| `t+20m` | Probe fallita | Quarantena 30m, tentativo 3 | `t+50m` |
| `t+50m` | Probe fallita | Quarantena 60m, tentativo 4 | `t+110m` |
| `t+110m` | Probe fallita | Quarantena 60m, limite raggiunto | `t+170m` |
| `t+170m` e oltre | Probe ancora fallita | Quarantena 60m | Ogni 60m |
| Prima probe riuscita | Chunk recuperato | Sano, tentativo azzerato | - |

**Note sui tempi:**

- Ogni scadenza reale decorre dal completamento della probe precedente, quindi
  può slittare per la durata della richiesta.
- Un cooldown FloodWait può rinviare ulteriormente la probe senza avanzare il
  tentativo di quarantena.
- Senza traffico, la scadenza non genera richieste Telegram: il bot resta
  idoneo finché una normale richiesta non avvia la probe.
- Non esiste un tempo massimo garantito di recupero. La prima probe riuscita
  riporta immediatamente il bot allo stato sano.

### A.4 - Macchina a stati di un singolo bot

```text
[QUARANTENA 5m]
        |
        v
   [HALF_OPEN] -- successo ------------------------> [SANO]
        |
        +-- timeout --> [QUARANTENA 15m]
                              |
                              v
                         [HALF_OPEN] -- successo ---> [SANO]
                              |
                              +-- timeout --> [QUARANTENA 30m]
                                                    |
                                                    v
                                               [HALF_OPEN]
                                                    |
                         successo --> [SANO] <------+-- timeout
                                                               |
                                                               v
                                                     [QUARANTENA 60m]
                                                               |
                                                               v
                                                          [HALF_OPEN]
                                                               |
                                    successo --> [SANO] <------+-- timeout
                                                               |
                                                               +--> nuova attesa 60m
```

Una probe annullata, soggetta a FloodWait o non idonea per il canale esce da
`half_open` senza avanzare il tentativo. Il membro resta in quarantena e torna
idoneo dopo l'eventuale cooldown applicabile.
