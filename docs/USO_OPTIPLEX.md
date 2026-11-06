# DiscFlow sull'OptiPlex

## Inserire DVD in serie

1. Apri la pagina DiscFlow già configurata (`/ripper`) sul computer con il lettore o tramite il proxy esistente.
2. Inserisci un DVD. DiscFlow sceglie il titolo più lungo, mostra le alternative, acquisisce l'MKV ed espelle il disco.
3. Appena il lettore si apre, inserisci il DVD successivo. La compressione può lavorare nello stesso momento.
4. Nella sezione **Compressione** scegli **Quadro P1000 · più veloce** oppure **CPU · qualità e file più piccoli**. Premi **Avvia compressione** una volta: la coda procede un film alla volta.
5. Per Jellyfin, scrivi il titolo corretto e l'anno facoltativo nella riga del film ancora in attesa, poi premi **Salva titolo**. DiscFlow crea `movie.nfo` dopo aver verificato il file finale. Se il titolo non è sicuro, lascia il campo vuoto e catalogalo in seguito.

## Controllare un film

Premi **Catalogo e lettore** nella sezione Film. Vedi al massimo otto film per
pagina, con i più recenti in alto; puoi cercare un titolo, un attore o un
regista e filtrare per genere. Premi **Apri nel lettore** per usare VLC o un
altro programma sul PC da cui apri il sito. Il film viene letto via rete senza
scaricarlo. Per un altro PC usa **Scarica playlist** e apri il piccolo file M3U
con il tuo lettore; serve Tailscale per raggiungere l'OptiPlex. Se manca un
lettore, nella scheda trovi lo ZIP ufficiale di VLC per Windows 64 bit, da
estrarre senza installazione. L'MKV originale resta disponibile con
**Scarica l'MKV originale** nella scheda del film.

Per vedere il film dal telefono Android, apri il catalogo in Chrome e usa
**Installa app**. Nella lista del telefono **Guarda nel browser** apre la scheda
e prepara la copia MP4 solo alla prima visione; l'originale resta intatto.
La preparazione usa CPU e Quadro P1000 per alcuni minuti e richiede spazio
aggiuntivo. Dopo, la riproduzione usa soltanto disco e rete. La visione nel
browser funziona attraverso la pagina pubblica già autenticata; per un lettore
esterno sul telefono serve Tailscale.

Titolo italiano, anno, regia, attori e generi vengono cercati automaticamente.
Le corrispondenze sicure rinominano il solo file finale e aggiornano `movie.nfo`
per Jellyfin. Se due film hanno lo stesso nome, la scheda propone i risultati:
scegli quello corretto. La copertina compare quando è disponibile su Wikipedia.
La vista esclude i sorgenti nella cartella predefinita.
La cartella è configurabile sull'OptiPlex con
`X99_RIPPER_COMPLETED_DIR` nel file `~/.config/x99-ripper/config`; scegli una
cartella dedicata ai film finali. Il cambio richiede il riavvio del servizio
web quando la coda è ferma.

**Verificato** significa che il file finale è presente, ha durata e tracce previste e che due campioni video sono stati decodificati senza errore. L'audio italiano, se presente, è predefinito. I sottotitoli italiani e inglesi sono selezionabili ma non partono da soli.

L'MKV acquisito e gli eventuali tentativi parziali restano nella cartella del film. DiscFlow non li elimina automaticamente. Controlla il film in Jellyfin prima di decidere se rimuovere qualcosa.

## Se compare un errore

- **Spazio insufficiente:** libera spazio nella cartella di destinazione e premi **Riprova lavori con errore**.
- **Codifica GPU fallita:** scegli **CPU** quando la coda è ferma, poi premi **Riprova lavori con errore**. Il tentativo parziale viene rinominato e conservato.
- **DVD non espulso:** controlla il lettore. Se l'MKV è già nella coda, non ripetere l'acquisizione.
- **Titolo sbagliato:** la scelta automatica usa la durata; non è affidabile per dischi con episodi, extra o più montaggi. Controlla **Titoli trovati nel disco** prima di usare il file finale.

## Versione e ripristino

La versione installata è basata sul branch GitHub `feat/discflow-ai-mvp`, tag `v0.12.1`. Il codice precedente è conservato nel tag `baseline/optiplex-working-0.6.2`; prima dell'aggiornamento è stata salvata anche una copia locale in `~/backup-discflow-20261106-2145/`. Per ripristinare, ferma il servizio web DiscFlow, copia i file dell'applicazione e dello stato da quel backup e riavvia il servizio. Non usare contemporaneamente la finestra desktop e la coda web.

DiscFlow usa MakeMKV e HandBrake già installati. Non include chiavi né funzioni per aggirare protezioni DRM.
