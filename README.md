# X99 Ripper 0.5.0

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

Sul secondo PC esegui `./install-web.sh` dopo aver installato la versione desktop. Il servizio web ascolta solo sull'indirizzo Tailscale del PC, alla porta 8766, e mostra la pagina `/ripper`. Inserendo un DVD, la pagina sceglie il titolo più lungo e avvia automaticamente estrazione, analisi crop, compressione e verifica; espelle il disco dopo l'estrazione. L'output resta nella cartella locale configurata e l'MKV intermedio viene rimosso solo dopo la verifica. Se il programma desktop è aperto, la pagina non avvia un lavoro concorrente: completa quel lavoro e chiudi la finestra desktop prima di usare l'automazione web.

Per notifiche Telegram, aggiungi `export X99_RIPPER_TELEGRAM_TOKEN=...` e `export X99_RIPPER_TELEGRAM_CHAT_ID=...` al file locale `~/.config/x99-ripper/config`, poi riavvia `systemctl --user restart x99-ripper-web.service`. Tieni quel file con permessi 600 e non inserirlo nel repository. Le notifiche segnalano completamento o errore; il funzionamento del ripper non dipende da Telegram. Il trasferimento futuro verso un NAS richiederà un percorso di destinazione e una verifica della copia prima della rimozione locale.

## Uso

1. Inserisci un DVD e premi **Rileva disco**. L'app propone il titolo più lungo; controlla durata e titolo e scegli il nome del film.
2. Premi **Estrai titolo selezionato**. L'MKV intermedio e `estrazione.log` vanno in `/mnt/x99/DVD_RIPPATI/NomeFilm/`. Terminata l'estrazione, l'app tenta di espellere il disco. Puoi inserire il successivo mentre HandBrake comprime il precedente.
   Il riquadro in alto mostra la fase attiva, il tempo trascorso e i byte scritti. Durante l'estrazione la percentuale deriva dalla dimensione attesa del titolo ed è una stima; durante la compressione arriva da HandBrake. Se il file non cresce per oltre 30 secondi, compare un messaggio. Gli errori di lettura del disco sono segnalati senza interrompere automaticamente il lavoro.
3. Dopo l'estrazione, l'app rileva il crop e accoda automaticamente la compressione. **Mostra dettagli** apre tracce, crop, anteprima e strumenti di recupero. **Mostra log** apre i messaggi tecnici. Il valore di crop applicato resta nel log.
4. L'app include **tutte** le tracce audio e i sottotitoli in italiano, spagnolo e inglese presenti nel MKV. Mette per primo e come predefinito l'audio italiano, se esiste. L'italiano viene codificato in AAC 160 stereo; le tracce spagnole e inglesi vengono copiate quando il codec lo consente, conservando i canali 5.1. Se la copia non è disponibile, vengono codificate in AAC, mantenendo 5.1 quando presente. Tutti i sottotitoli restano selezionabili ma spenti all'avvio e non impressi nel video. Se l'italiano manca, diventa predefinita la prima lingua presente nell'ordine spagnolo, inglese.
5. La coda comprime un film alla volta. Il file finale è `NomeFilm_H265.mkv`. `compressione.log` conserva il log completo. L'app controlla codec, durata, audio, sottotitoli e flag prima di rinominare il file `.parziale.mkv` in finale.
6. Dopo la verifica e la creazione del file finale, l'app elimina automaticamente **solo l'MKV intermedio che ha estratto lei stessa**. Non elimina il DVD originale né gli MKV importati da altre cartelle. Se la rimozione fallisce, il lavoro resta **verificato** e l'app segnala che l'intermedio è ancora presente; il pulsante **Rimuovi intermedio rimasto** consente di riprovare con conferma.

Puoi provare il percorso di scansione e compressione con **Importa MKV esistente**, senza inserire un disco. La cartella dell'MKV importato resta intatta.

## Spazio ed errori

Prima dell'estrazione richiede spazio pari almeno alla dimensione dichiarata del titolo più 8 GiB, e comunque almeno 12 GiB. Prima della compressione richiede almeno 4 GiB o un quarto della dimensione dell'MKV sorgente. Sono margini prudenti, non una stima esatta dell'output: controlla lo spazio libero mostrato nell'app. Un errore lascia sorgente, file parziale e log disponibili. Se la compressione si interrompe, la schermata principale mostra **Riprova compressione**: l'app rinomina il file `.parziale.mkv` in `.interrotto-DATA.mkv`, lo conserva e avvia automaticamente un nuovo tentativo dall'MKV originale. La coda è salvata in `~/.local/state/x99-ripper/jobs.json` e riprende i lavori in attesa dopo il riavvio. Il programma ignora il segnale SIGHUP del terminale; per un avvio indipendente dal terminale usa comunque la voce nel menu applicazioni.

## Limiti di questa prima versione

La scansione di un DVD reale è riuscita; l'estrazione completa, l'espulsione e la compressione di un film intero non sono ancora state convalidate come ciclo unico. La codifica breve è stata provata con un MKV già presente sul Samsung e con un MKV di prova a tre lingue: audio italiano predefinito, audio spagnolo/inglese conservato e tre sottotitoli spenti. La scelta del titolo principale usa la durata come proposta, non una garanzia. L'anteprima crop usa un fotogramma a metà film: per pellicole con barre variabili controlla altri punti con un lettore video. Nessuna funzione gestisce chiavi o implementa procedure per aggirare protezioni DRM; l'app usa soltanto i comandi standard installati.
