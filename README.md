# DiscFlow 0.11.0 · coda DVD, catalogo e lettore esterno

La pagina include **Catalogo e lettore**: mostra nel browser i file finali, con
ricerca, filtro per genere, otto film per pagina e i più recenti in cima.
**Apri nel lettore** avvia il film in streaming sul dispositivo che apre il
sito. Sul PC Linux configurato, il gestore `discflow-player://` sceglie VLC,
mpv, Celluloid, SMPlayer o Parole tra quelli presenti. Per altri PC è
disponibile **Scarica playlist**: il file M3U è minuscolo e contiene il
collegamento al film, non il video. Il dispositivo deve raggiungere l'OptiPlex
tramite Tailscale. Il link scade dopo 24 ore e consente solo la lettura del
film scelto. Lo stream supporta la ricerca nella timeline.

Se manca un lettore, la scheda film collega lo [ZIP ufficiale di VLC per
Windows 64 bit](https://download.videolan.org/pub/videolan/vlc/3.0.24/win64/vlc-3.0.24-win64.zip)
(circa 83 MB). Basta estrarlo e avviare VLC, senza installazione.

Per abilitare l'apertura con un clic su un PC Linux con Tailscale:

```bash
./install-player-handler.sh IP_TAILSCALE_OPTIPLEX
```

Sull'OptiPlex configura `X99_RIPPER_TAILSCALE_HOST` con il suo IP Tailscale e
riavvia solo il servizio web DiscFlow. Su quell'interfaccia il servizio
accetta soltanto URL del film firmati e a scadenza. La vecchia copia MP4
eventualmente già preparata resta conservata; la nuova interfaccia non ne
crea altre.

DiscFlow cerca automaticamente titolo italiano, anno, regista, attori, generi,
identificativo IMDb e copertina usando Wikidata e Wikipedia. Confronta il nome
del DVD e la durata del film; rinomina il file finale e crea `movie.nfo` per
Jellyfin solo quando la corrispondenza è sufficientemente chiara. Nei casi
ambigui propone i risultati nella scheda del film e chiede di scegliere.
Non serve una chiave API. Le copertine dipendono dalla disponibilità su
Wikipedia; i film senza immagine restano senza copertina. Nessun file video
viene eliminato automaticamente.

Per impostazione predefinita legge la cartella di
output e mostra solo i file `*_H265.mkv`, escludendo gli MKV sorgente. Per una
cartella distinta imposta `X99_RIPPER_COMPLETED_DIR` nella configurazione
locale dell'OptiPlex e riavvia il servizio web a coda ferma. In una cartella
personalizzata mostra tutti gli MKV presenti: scegline una dedicata ai finali.

Per l’uso quotidiano vedi [DiscFlow sull’OptiPlex](docs/USO_OPTIPLEX.md).

La pagina web separa lettore e compressione. Puoi scegliere **CPU** (x265
10 bit, slow, RF22) o **Quadro P1000** (NVENC HEVC 10 bit, medium, CQ28)
prima di avviare la coda. Se la GPU fallisce, il lavoro resta in errore con
MKV sorgente e parziale conservati: seleziona CPU e premi **Riprova lavori con
errore**. La scelta GPU riguarda la coda web; la finestra desktop continua a
usare la CPU.

### Misura sulla Quadro P1000 dell'OptiPlex

Su un campione di 60 secondi al minuto 30 di un DVD già acquisito, senza
audio né sottotitoli per isolare il video:

| Encoder | Tempo | Dimensione |
| --- | ---: | ---: |
| CPU x265 10 bit RF22 | 174,3 s | 4,14 MB |
| Quadro P1000 NVENC 10 bit CQ28 | 2,7 s | 4,70 MB |

Il campione GPU è circa 65 volte più rapido e 14% più grande. Il confronto
vale per quel tratto del film: non predice esattamente dimensione o qualità di
ogni DVD. Un secondo campione GPU con audio italiano/inglese e sottotitoli
IT/EN ha superato la verifica di tracce, flag e decodifica. Per ripetere la
misura su un'altra scena:

```bash
python3 benchmark_nvenc.py --start 1800 --gpu-quality 28 \
  '/percorso/al/sorgente.mkv' "$HOME/Videos/benchmark-nvenc-$(date +%Y%m%d-%H%M%S)"
```

Per trasferire MKV già acquisiti dalla vecchia coda desktop alla coda web,
prima ferma il desktop e il servizio web, salva una copia dei due file di stato,
poi esegui dal repository con la cartella di output configurata:

```bash
X99_RIPPER_OUTPUT_DIR="$HOME/Videos/X99-Ripper" python3 migrate_desktop.py
```

Il trasferimento non elimina sorgenti. Archivia il parziale e marca i vecchi
lavori desktop come trasferiti, così non partono due codifiche dello stesso film.

Questa versione conserva la precedente: il branch `main` e il tag
`baseline/optiplex-working-0.6.2` restano disponibili. Il branch MVP è
`feat/discflow-ai-mvp`. Attendi la fine dei lavori attivi prima di installare.

## Funzioni e test

- La pagina web acquisisce i DVD e comprime la coda in processi separati. Dopo
  l'espulsione puoi inserire il disco successivo mentre HandBrake lavora.
- Audio italiano per primo, inglese conservato; sottotitoli IT/EN soft e mai
  predefiniti o impressi nel video.
- Un'estrazione interrotta resta come errore. **Riprova errori** recupera un
  solo MKV sorgente valido e conserva il tentativo parziale precedente.
- Gli MKV intermedi non vengono eliminati automaticamente. Nel desktop la
  rimozione manuale richiede conferma.
- Nella pagina web puoi inserire titolo e anno per Jellyfin prima della
  compressione. Dopo la verifica viene creato `movie.nfo`, senza sovrascrivere
  un file esistente. Il programma non inventa metadati se lasci il campo vuoto.
- `benchmark_nvenc.py` conserva campioni, log e risultati in una cartella nuova.

Test locali:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
python3 -m py_compile ripper.py web.py benchmark_nvenc.py migrate_desktop.py
```

Quando la coda attiva è terminata, aggiorna l'OptiPlex:

```bash
mkdir -p "$HOME/backup-discflow-$(date +%Y%m%d)"
cp -a "$HOME/.local/share/x99-ripper" "$HOME/.local/state/x99-ripper" "$HOME/backup-discflow-$(date +%Y%m%d)/"
cd "$HOME/.local/src/discflow-ai-mvp"
git pull --ff-only
./install.sh
./install-web.sh
```

Il desktop e la pagina web non comandano il lettore contemporaneamente: con la
finestra desktop aperta, i comandi web restano sospesi. La coda web richiede
**Avvia compressione della coda**; puoi premerlo anche mentre acquisisci un
altro DVD. Per Jellyfin, configura la libreria sulla cartella di destinazione
già scelta; questo MVP non cambia la configurazione del server Jellyfin.

Tempi e dimensioni dei campioni non misurano da soli la qualità visiva.
Whisper non è attivato in questa versione: sulle tracce finora viste le lingue risultano
etichettate, quindi manca un caso reale in cui usarlo migliorerebbe la scelta.
Nessun modello viene scaricato o eseguito automaticamente.

Applicazione Linux con interfaccia grafica per estrarre un titolo DVD con MakeMKV e comprimerlo con HandBrake. Versione iniziale per Ubuntu 26.04.1 sull'X99.

## Requisiti verificati su questo PC

- MakeMKV Snap 1.18.3: `makemkvcon` in `/snap/bin`; la sintassi locale conferma `info dev:/dev/sr0` e `mkv dev:/dev/sr0 <titolo> <cartella>`.
- HandBrake Flatpak `fr.handbrake.ghb` 1.11.2, con `HandBrakeCLI` interno; la sintassi locale e una codifica breve confermano HEVC 10-bit, preset `slow`, RF22, selezione audio, crop personalizzato, sottotitoli senza default né burn.
- Python 3 con Tkinter, `ffprobe`, `ffmpeg`, `eject`.
- Samsung montato in `/mnt/x99`; cartella `/mnt/x99/DVD_RIPPATI` scrivibile. Al controllo erano liberi circa 81 GiB.
- Lettore ottico `/dev/sr0`.

Il collegamento Snap `makemkv:removable-media` risultava scollegato, ma un controllo nella shell dello Snap vede la cartella. Se l'estrazione dà errore di accesso a `/mnt/x99`, collegarlo una volta con:

```bash
sudo snap connect makemkv:removable-media
```

## Licenza MakeMKV

MakeMKV è un programma separato e non è incluso in questo repository. Secondo la [pagina ufficiale](https://www.makemkv.com/buy/index.html), l'uso per DVD/AVCHD è gratuito; le funzioni Blu-ray sono shareware. La licenza acquistata non è mensile e non scade. Per l'eventuale chiave beta aggiornata, usa sempre l'[annuncio ufficiale](https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053). Questa app non genera, distribuisce o salva chiavi di MakeMKV.

Quando MakeMKV identifica un Blu-ray, l'app mostra un avviso e un pulsante che apre la pagina ufficiale della chiave. L'avviso scompare dopo una prima estrazione Blu-ray riuscita; se una successiva scansione o estrazione segnala una chiave scaduta, ricompare. Viene salvata soltanto la data dell'ultima estrazione Blu-ray riuscita in `~/.local/state/x99-ripper/bluray-verification.json`, mai la chiave. Il lettore `/dev/sr0` verificato sull'X99 è un'unità DVD: per i Blu-ray serve anche un lettore Blu-ray compatibile. Se la scansione non trova titoli, l'app lo segnala separatamente dal controllo della licenza.

## Avvio

Scompatta o copia questa cartella e avvia `./run.sh`, oppure installa una voce nel menu applicazioni:

```bash
./install.sh
```

L'installazione copia l'app in `~/.local/share/x99-ripper` e crea solo il lanciatore utente. Non modifica Docker, Ollama o Tailscale. Per aggiornare, riesegui `./install.sh` dalla nuova versione. Su un altro PC puoi scegliere la cartella di destinazione senza modificare il codice: `X99_RIPPER_OUTPUT_DIR="$HOME/Videos/X99-Ripper" ./install.sh`. La cartella deve già esistere ed essere scrivibile; la scelta viene salvata solo sul PC in `~/.config/x99-ripper/config`. Il lettore predefinito è `/dev/sr0` e si può cambiare con `X99_RIPPER_DEVICE` allo stesso modo. Se `HandBrakeCLI` è installato come pacchetto Ubuntu, l'app lo usa direttamente; altrimenti usa il Flatpak verificato sull'X99.

Se una vecchia versione è già aperta e sta lavorando, lasciala finire: l'aggiornamento installato avrà effetto al prossimo avvio. Durante la lavorazione in corso puoi aprire il monitor di sola lettura con `python3 ~/.local/share/x99-ripper/monitor.py`; chiudere quel monitor non interrompe l'estrazione o la compressione.

### Installazione su un secondo PC

Installa `python3-tk`, `handbrake-cli`, `ffmpeg`, `eject` e MakeMKV, poi crea una cartella capiente e avvia l'installazione con `X99_RIPPER_OUTPUT_DIR="$HOME/Videos/X99-Ripper" ./install.sh`. L'app compare nel menu del secondo PC. Per usarla tramite desktop remoto, accedi al PC con un servizio già configurato sulla tua rete privata e apri X99 Ripper dal suo menu. Credenziali, chiavi SSH e chiavi MakeMKV restano nelle configurazioni locali e non vanno nel repository.

### Pagina web per il DVD in serie

Sul secondo PC esegui `./install-web.sh` dopo aver installato la versione desktop. Il servizio web mostra la pagina `/ripper` alla porta 8766. Se il proxy Cloudflare è su un altro PC della stessa LAN, imposta `X99_RIPPER_WEB_HOST` all'indirizzo LAN del computer con il lettore e `X99_RIPPER_ALLOWED_PROXY` all'indirizzo LAN del proxy prima di installare; le richieste da altri computer ricevono 403. Inserendo un DVD, la pagina sceglie il titolo più lungo, estrae l'MKV, espelle il disco e lo mette **in attesa**. Scegli il limite di 5 o 10 DVD nella pagina e continua a inserire dischi. Quando hai finito, premi **Avvia compressione**: HandBrake analizza crop, audio e sottotitoli e lavora un film alla volta, anche durante la notte. La pagina mostra insieme la coda web e i lavori già avviati nel programma desktop: per ogni film indica titolo scelto, durata, dimensione, stato, avanzamento ed eventuale tempo stimato. Durante la scansione web mostra tutti i titoli rilevati e segnala quello scelto perché più lungo. La pagina mostra progresso ed errori; se un film fallisce, la coda passa al successivo. **Riprova lavori con errore** conserva il tentativo parziale prima di rimetterlo in coda. L'output e l'MKV intermedio restano nella cartella locale configurata anche dopo la verifica. La coda web è salvata in `~/.local/state/x99-ripper/web-state.json`. Se il programma desktop è aperto, la pagina ne mostra lo stato ma non avvia un lavoro concorrente: completa quel lavoro e chiudi la finestra desktop prima di usare l'automazione web.

Per notifiche Telegram, apri il terminale dell'OptiPlex ed esegui `python3 ~/.local/share/x99-ripper/configure-telegram.py`. Inserisci il token del bot personale quando richiesto: non appare sullo schermo. Lo script ti chiede di inviare un breve codice al bot, individua la chat e manda un messaggio di prova. Token e ID chat vengono salvati solo nel file locale `~/.config/x99-ripper/config` con permessi 600, mai nel repository. Le notifiche segnalano completamento o errore; il funzionamento del ripper non dipende da Telegram. Il servizio `x99-ripper-notify-desktop.service` controlla anche i lavori avviati nella finestra desktop e avvisa quando un film entra in coda, richiede conferma, termina o dà errore. Rilegge la configurazione automaticamente. Il trasferimento futuro verso un NAS richiederà un percorso di destinazione e una verifica della copia prima della rimozione locale.

## Uso

1. Inserisci un DVD e premi **Rileva disco**. L'app propone il titolo più lungo; controlla durata e titolo e scegli il nome del film.
2. Premi **Estrai titolo selezionato**. L'MKV intermedio e `estrazione.log` vanno in `/mnt/x99/DVD_RIPPATI/NomeFilm/`. Terminata l'estrazione, l'app tenta di espellere il disco. Puoi inserire il successivo mentre HandBrake comprime il precedente.
   Il riquadro in alto mostra la fase attiva, il tempo trascorso e i byte scritti. Durante l'estrazione la percentuale deriva dalla dimensione attesa del titolo ed è una stima; durante la compressione arriva da HandBrake. Se il file non cresce per oltre 30 secondi, compare un messaggio. Gli errori di lettura del disco sono segnalati senza interrompere automaticamente il lavoro.
3. Dopo l'estrazione, l'app rileva il crop e accoda automaticamente la compressione. **Mostra dettagli** apre tracce, crop, anteprima e strumenti di recupero. **Mostra log** apre i messaggi tecnici. Il valore di crop applicato resta nel log.
4. L'app include le tracce audio e i sottotitoli in italiano e inglese presenti nel MKV. Mette per primo e come predefinito l'audio italiano, se esiste. L'italiano viene codificato in AAC 160 stereo; le tracce inglesi vengono copiate quando il codec lo consente, conservando i canali 5.1. Se la copia non è disponibile, vengono codificate in AAC, mantenendo 5.1 quando presente. Tutti i sottotitoli restano selezionabili ma spenti all'avvio e non impressi nel video. Se l'italiano manca, diventa predefinita la prima lingua presente in inglese.
5. La coda comprime un film alla volta. Il file finale è `NomeFilm_H265.mkv`. `compressione.log` conserva il log completo. L'app controlla codec, durata, audio, sottotitoli e flag prima di rinominare il file `.parziale.mkv` in finale.
6. Dopo la verifica, l'app conserva l'MKV intermedio. Il pulsante **Rimuovi intermedio** permette di eliminarlo soltanto dopo una conferma esplicita nella versione desktop.

Puoi provare il percorso di scansione e compressione con **Importa MKV esistente**, senza inserire un disco. La cartella dell'MKV importato resta intatta.

## Spazio ed errori

Prima dell'estrazione richiede spazio pari almeno alla dimensione dichiarata del titolo più 8 GiB, e comunque almeno 12 GiB. Prima della compressione richiede almeno 4 GiB o un quarto della dimensione dell'MKV sorgente. Sono margini prudenti, non una stima esatta dell'output: controlla lo spazio libero mostrato nell'app. Un errore lascia sorgente, file parziale e log disponibili. Se la compressione si interrompe, la schermata principale mostra **Riprova compressione**: l'app rinomina il file `.parziale.mkv` in `.interrotto-DATA.mkv`, lo conserva e avvia automaticamente un nuovo tentativo dall'MKV originale. La coda è salvata in `~/.local/state/x99-ripper/jobs.json` e riprende i lavori in attesa dopo il riavvio. Il programma ignora il segnale SIGHUP del terminale; per un avvio indipendente dal terminale usa comunque la voce nel menu applicazioni.

## Limiti di questa prima versione

La scansione di un DVD reale è riuscita; l'estrazione completa, l'espulsione e la compressione di un film intero non sono ancora state convalidate come ciclo unico. La codifica breve della versione precedente è stata provata con un MKV a tre lingue; il nuovo filtro IT/EN è coperto dai test automatici ma richiede ancora una prova su DVD reale. La scelta del titolo principale usa la durata come proposta, non una garanzia. L'anteprima crop usa un fotogramma a metà film: per pellicole con barre variabili controlla altri punti con un lettore video. Nessuna funzione gestisce chiavi o implementa procedure per aggirare protezioni DRM; l'app usa soltanto i comandi standard installati.
