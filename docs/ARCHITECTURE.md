# Architecture de la Raspberry Pi « HiFi »

> Récepteur audio de salon **AirPlay + Bluetooth** avec écran tactile « Now Playing »
> personnalisé. Document de référence — état vérifié sur la machine le **2026-06-06**.

---

## 0. Accès & identité

| | |
|---|---|
| Hostname | `hifi` |
| IP (WiFi) | `192.168.129.12/23` (bail DHCP dynamique) |
| Passerelle / DNS | `192.168.128.1` (box Proximus) |
| MAC WiFi (`wlan0`) | `B8:27:EB:72:EC:6D` *(OUI Raspberry Pi, non randomisée → utilisable pour une réservation DHCP)* |
| Utilisateur | `hifi` — mot de passe = **un seul espace** `" "` |
| Accès | SSH (`ssh.service` actif, activé) |

---

## 1. Rôle & vue d'ensemble

La Pi est un **récepteur audio dédié**. L'utilisateur n'emploie **que** : AirPlay,
Bluetooth, et l'écran tactile maison. L'interface web de moOde, MPD et le partage
de fichiers ne sont **jamais** utilisés (et sont masqués — voir §8).

Deux sources audio mutuellement exclusives (**AirPlay XOR Bluetooth**) sortent par
**HDMI → DAC externe**. Un écran DSI 7" affiche pochette / titre / progression /
transport, ou une horloge + météo au repos.

```
   iPhone ──AirPlay 1──┐
                       │   (shairport-sync)
                       ├──►  ALSA "_audioout"  ──►  HDMI (card1 vc4hdmi, IEC958 44.1k)
                       │      = type plug             │
   Téléphone ─A2DP/SBC─┘   (bluez-alsa aplay)         └──► adaptateur HDMI→VGA+jack
              (Bluetooth)                                  (DAC passif alimenté par le 5V HDMI)
                                                                │
                                                                └──► Ampli / enceintes

   Écran DSI 800×480 (KMSDRM /dev/dri/card0)  ◄──  nowplaying.py (pygame, user hifi)
        ▲                                              │  lit les métadonnées :
        └── tactile : transport, volume, bascule ──────┘  pipe shairport / DACP / AVRCP / météo
```

**Une seule source active à la fois** : `shairport-sync` et `bluealsa-aplay`
veulent tous deux le périphérique ALSA `_audioout`. Le script `setmode.sh` arbitre.

---

## 2. Matériel

| Élément | Détail |
|---|---|
| Carte | **Raspberry Pi 3 Model A+ Rev 1.0** |
| CPU | Quad-core ARM **Cortex-A53 @ 1.4 GHz** (aarch64) |
| RAM | **415 MB** utilisables (contrainte majeure — pas le CPU) |
| Température | ~49 °C, `throttled=0x0` (aucun throttling) |
| Écran | Panneau officiel **DSI 7" 800×480** tactile (capacitif) |
| Rétroéclairage | `/sys/class/backlight/10-0045` (max 255) — `brightness` inscriptible par le groupe `video`, **sans sudo** |
| Stockage | microSD : `/` ext4 14 G (59 % utilisé), `/boot/firmware` FAT 510 M |
| Sortie audio | **Pas de DAC USB/I²S.** Adaptateur **passif HDMI→VGA avec jack 3.5mm + DAC intégré** (alimenté uniquement par la broche 5V du HDMI) |
| Cartes ALSA | `card0` Headphones (PWM bcm2835, **contournée**) · `card1` **vc4hdmi** (`MAI PCM i2s-hifi-0`, utilisée) |
| Réseau | WiFi onboard **BCM43455** 2.4 GHz (antenne partagée avec le BT) — pas d'Ethernet sur le 3A+ |

> ⚠️ Le DAC est dans l'adaptateur passif : c'est **lui** qui fixe le plafond de
> qualité analogique. L'adaptateur n'assure **jamais** le hotplug HDMI (HPD) — d'où
> le forçage côté noyau (§5).

---

## 3. Système & démarrage

| | |
|---|---|
| Distribution | **moOde audio** (base **Debian 13 « trixie » 64-bit**) |
| Noyau | `6.18.29+rpt-rpi-v8 aarch64` |
| Pilote graphique | **Full KMS** (`dtoverlay=vc4-kms-v3d`) → DRM `/dev/dri/card0` |
| Boot | **~21 s** (3.0 s noyau + 18.0 s userspace) — était 45 s avant slimming |
| Runtimes | Python **3.13.5**, pygame **2.6.1** (SDL **2.32.4**), Pillow **11.1.0**, python-dbus **1.4.0** |
| Swap | fichier `/var/swap` 4 GB, ~34 MB utilisés (`swappiness=60`) |

**`/boot/firmware/cmdline.txt`** (forçage HDMI pour l'audio, ligne unique) :
```
… consoleblank=0 video=HDMI-A-1:1920x1080@60e drm.edid_firmware=HDMI-A-1:edid/hifi-hdmi.bin
```
- `…@60**e**` : force-**active** le connecteur HDMI même sans HPD (l'adaptateur passif
  ne signale jamais le branchement).
- `drm.edid_firmware=…hifi-hdmi.bin` : EDID **256 octets sur mesure** (généré par
  `apply_hdmi_force.py`, dans `/lib/firmware/edid/`) contenant un **bloc audio
  CEA-861** (LPCM 2ch 32/44.1/48 kHz). Sans lui, KMS émettrait un mode **DVI = pas
  d'audio**.

> **Conflit double-connecteur (résolu) :** forcer le HDMI expose **deux** sorties sur
> `card0` : `DSI-1` *et* `HDMI-A-1`, toutes deux `connected`. SDL choisit l'écran 0
> par défaut (= HDMI invisible) → le DSI restait figé. `nowplaying.py` (`init_display`)
> énumère `get_desktop_sizes()` et sélectionne **l'index dont la taille == (800,480)**
> = le DSI, robuste quel que soit l'ordre d'énumération.

---

## 4. Réseau & robustesse WiFi

- **Profil utilisé : `Proximus-Limited-892724`** (WPA2 / 2.4 GHz, `band=bg`), autoconnect
  priorité 999. L'autoconnect est **coupé sur tous les autres profils** (le « thrash »
  multi-profils cassait scan/association). Éviter `Proximus-Home` (transition
  WPA2/WPA3 + 5 GHz → décroche le 3A+).
- **Power-save WiFi désactivé de façon persistante** : keyfile `powersave=2` +
  drop-in NM `conf.d/…-wifi-powersave…` + dispatcher (chemin complet `/sbin/iw`).
  → évite les micro-coupures AirPlay (le BCM43455 sommeillait entre balises).
- **Watchdog : `wifi-watchdog.service`** (`/usr/local/bin/wifi-watchdog.sh`) — ping la
  passerelle toutes les **30 s** :
  - 5 échecs → `nmcli con up` ;
  - 10 échecs → radio off/on ;
  - **~45 min hors-ligne (≥ 90 échecs) → un reboot** rate-limité (garde 30 min via
    `/var/lib/wifi-watchdog.lastreboot`).
  - **Le reboot est SAUTÉ si du Bluetooth joue de la musique** (`bt_playing()` :
    substream `hw_params` ouvert — PRIMAIRE, marche même `org.bluez` éteint — ou
    BlueZ `MediaTransport1=active`/`MediaPlayer1=playing`). Comme le watchdog ne se
    déclenche que WiFi coupée, un audio local actif ⟹ forcément BT.
  - Log sur la partition FAT : `/boot/firmware/wifi-watchdog.log` (lisible depuis
    Windows en mettant la SD dans le PC).

> **Cause racine récurrente : c'est la BOX Proximus qui se bloque, pas la Pi.** Après
> une coupure de courant la box revient dans un mauvais état 2.4 GHz. **Remède : power-cycle
> de la box** (débrancher 30 s, attendre 3-4 min). Le watchdog ne peut PAS réparer une
> box bloquée. Pistes durables : (1) USB→Ethernet (supprime le 2.4 GHz), (2) canal
> 2.4 GHz fixe 1/6/11, (3) réservation DHCP `B8:27:EB:72:EC:6D → .12`.

---

## 5. Chaîne audio (signal path)

**Le point clé = bit-perfect par HDMI/IEC958 vers le DAC externe**, la carte jack
PWM onboard (bruitée) étant contournée.

```
shairport-sync ─┐
                ├─► ALSA "_audioout"  ──►  hw:CARD=vc4hdmi,DEV=0  ──►  HDMI 44.1 kHz IEC958
bluealsa-aplay ─┘   (/etc/alsa/conf.d/_audioout.conf)
```

`/etc/alsa/conf.d/_audioout.conf` :
```
pcm._audioout { type plug ; slave.pcm "hw:CARD=vc4hdmi,DEV=0" }
```
- `type plug` est **obligatoire** : le hw HDMI n'accepte que `IEC958_SUBFRAME_LE`
  ; `plug` enrobe le cadrage IEC958 (sans rééchantillonnage en pratique).
- État live vérifié : `card1/pcm0p/sub0` ouvert en **`IEC958_SUBFRAME_LE`, 2 ch,
  44100 Hz** → AirPlay 44.1 ouvert **nativement, sans SRC**.

### 5.1 AirPlay — `shairport-sync.service`
- Version **4.3.7** (soxr + convolution + dbus/mpris + metadata + mqtt).
- **AirPlay 1 / classique** (`nqptp` inactif → pas d'AirPlay 2). La latence ~2 s est
  **inhérente au protocole** (buffer de synchro multiroom annoncé par iOS), non
  réductible sans casser la synchro. Levier logiciel :
  `audio_backend_buffer_desired_length_in_seconds = 0.5` (anti-saccade ; 0.2 saccadait).
- `interpolation = "auto"` (soxr si le CPU suit, sinon basic).
- **Volume = volume LOGICIEL shairport** (intentionnel, **à conserver**) :
  `mixer_control_name` commenté → pas de mixer hardware → atténuation flottante
  propre avant la sortie. Bit-perfect à 0 dB, légère perte de résolution inaudible
  en-dessous. C'est ce qui permet de régler le son **depuis l'iPhone** sans toucher
  l'ampli. Il n'existe pas de moyen d'avoir *à la fois* volume iPhone *et*
  bit-perfect à tous les niveaux sur ce matériel.

> **Gestion de la conf :** moOde possède `/etc/shairport-sync.conf` (valeurs en base
> SQLite `cfg_airplay`, réécrites par `sed` via `apl-config.php`/`autocfg.php`). Mais
> le worker + l'UI web sont **masqués** → aucune régénération ne tourne → **les éditions
> directes du fichier persistent**. Par prudence, mettre à jour fichier **ET** base.
> Toujours `systemctl restart shairport-sync` après édition (une erreur de syntaxe le
> met en fail-loop).

### 5.2 Bluetooth — pile bluez-alsa (A2DP sink)
- Unités (toutes `static`, démarrées par `setmode.sh`, **pas** au boot) :
  - `bt-bluealsa.service` → `bluealsa -p a2dp-sink`
  - `bt-aplay.service` → `bluealsa-aplay --profile-a2dp --pcm=_audioout`
  - `bt-agent.service` → `bt-agent --capability=NoInputNoOutput` (appairage *Just-Works*)
- **iPhone = SBC** (bluez-alsa offre SBC/aptX/LDAC/Opus mais **pas AAC** ; Apple ne fait
  qu'AAC/SBC → SBC 44.1 kHz). Latence BT ~150-200 ms ⟹ **préférer le BT quand la
  latence compte** (vidéo sur le téléphone), l'AirPlay quand la synchro/qualité priment.
- `bt-*` ont `TimeoutStopSec=3` ; `bt-agent` a `KillSignal=SIGKILL` +
  `SuccessExitStatus=143 SIGTERM SIGKILL` (il se bloque *uniquement* sur le chemin
  d'arrêt systemd en désenregistrant son agent D-Bus → SIGKILL = arrêt instantané).

---

## 6. Bascule de source — `setmode.sh`

`/opt/nowplaying/setmode.sh {airplay|bluetooth|status}` (via `sudo`) arbitre qui
possède `_audioout`. Appelé en `Popen` non bloquant par `nowplaying.py` (pilule du
mode sur l'écran de repos + puce de bascule en lecture) et réconcilié au démarrage.

Robustesse (le **BCM43455 BT peut se bloquer** — `bluetoothctl` *et* `btmgmt` figent
alors pour toujours) :
- **chaque** appel adaptateur est enrobé de `timeout` ;
- bascules **sérialisées** par `flock` sur `/run/nowplaying-mode.lock` ;
- `bt_start` s'auto-répare une fois (`rfkill` block/unblock + `restart hciuart`) et,
  en cas d'échec, **retombe sur AirPlay** (jamais d'état cassé) ; idempotent (un re-tap
  ne coupe pas un flux A2DP actif) ;
- côté AirPlay : on éteint l'adaptateur **à travers bluetoothd** (`power off` via
  `bluetoothctl`, **jamais** `btmgmt power off` qui wedge le contrôleur) → « Hifi
  Bluetooth » disparaît des téléphones ;
- `bt_trust_paired` (`bluetoothctl trust`) → reconnexion auto sans réappairage ;
- `bt_volume` épingle `amixer -c Headphones sset PCM 0dB` à chaque passage BT.
- État courant : fichier `/run/nowplaying-mode` (= `airplay` actuellement).

---

## 7. Affichage « Now Playing » — `nowplaying.py`

Interface **pygame en mode immédiat** sur framebuffer **KMSDRM**, sans serveur X.

### 7.1 Service & environnement — `nowplaying.service`
```ini
ExecStartPre=/bin/sleep 3
ExecStart=/usr/bin/python3 /opt/nowplaying/nowplaying.py
User=hifi   Group=hifi   SupplementaryGroups=video render audio
Restart=always   RestartSec=3   TimeoutStopSec=5
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=SDL_VIDEO_KMSDRM_DEVICE=/dev/dri/card0
Environment=SDL_VIDEO_DOUBLE_BUFFER=0
Environment=SDL_AUDIODRIVER=dummy
Conflicts=localdisplay.service        # ⚠ voir §8
After=network.target shairport-sync.service
```
- Tourne en **`hifi`** (pas root) ; appartenance aux groupes `video`/`render`/`audio`
  → accès DRM + rétroéclairage sans sudo.
- **Seul détenteur de `/dev/dri/card0`** (vérifié : `fuser` → un seul `python3`).

### 7.2 Structure (3 fichiers dans `/opt/nowplaying/`)
| Fichier | Rôle |
|---|---|
| `nowplaying.py` (~95 ko) | logique : init écran, boucle de rendu, dessins `draw_*`, threads workers, gestion tactile |
| `npconfig.py` | **données pures** (importé `from npconfig import *`) : géométrie 800×480, palette, seuils, position des boutons/fader/pilules, météo (Liège 50.63, 5.57), veille |
| `npstate.py` | **`State` thread-safe** unique : un verrou ; les workers écrivent, la boucle lit via `snapshot()` (dict, jamais de verrou pendant le dessin) |

### 7.3 Threads workers (démons, lancés au démarrage)
| Thread | Source | Détail |
|---|---|---|
| `metadata` | **pipe métadonnées shairport** (bloquant) | titre/artiste/album/pochette + codes SSNC (`pbeg`/`prsm`→play, `pend`/`pfls`→stop). Construit les visuels (PIL : coins arrondis r=18 + ombre `GaussianBlur`) dans un thread |
| `dacpstate` | **DACP** du téléphone (long-poll) | `playstatusupdate` → octet `caps` (pause/play). **Bonus** : sur cet iPhone la requête renvoie souvent 400 → backoff exponentiel silencieux |
| `btmeta` | **BlueZ `MediaPlayer1`** (AVRCP, 1 s) | en mode BT : Status/Position/Track ; ms→frames via 44100 pour réutiliser la barre de progression |
| `weather` | **Open-Meteo** (Liège) | temp/code/min-max/lever-coucher/horaire ; refresh ~15 min |
| `hwquality` | `/proc/asound/.../hw_params` + BlueZ | badge live : rate/format réels (AirPlay & BT) + codec BT (`Codec` sur `MediaTransport1`) |

### 7.4 Boucle de rendu & performances
- **Événementielle** : `pygame.event.wait(wait_ms)` (pas de redraw plein-écran
  constant). `wait_ms` adaptatif : 1000 ms repos / 1000 ms lecture stable / 50 ms
  marquee (20 fps) / 33 ms transition·volume (30 fps) / 120 ms anim EQ.
- Garde `sig`/`need` : `pygame.display.update()` n'est appelé **que** si quelque chose
  a changé → tap lu en ~1 ms.
- **Optimisation CPU « glowcache » (2026-06-06)** : les halos `soft_glow` (deux passes
  `smoothscale`) de la pochette (bloom + anneau) et du curseur de progression étaient
  recalculés **à chaque frame** → pics ~75 % du thread de rendu pendant les rafales
  marquee/transition. Désormais **mémoïsés** par `(taille, accent)` (constants par
  piste) et simplement blittés ; marquee passé 30→20 fps.
  → **CPU du thread de rendu 13.4 % → ~2-3 % stable** ; pic supprimé à la racine.
  Sauvegarde live `/opt/nowplaying/nowplaying.py.bak.glowcache`.

> **Décision perf :** l'objectif performance est atteint (~2 % CPU, ~95 MB, jamais de
> crash). Une réécriture en moteur retenu (dirty-rect) a été **prototypée et validée**
> (`ui_engine.py`, 17/17 tests sur la Pi) mais **mise de côté** : plus rien à gagner en
> CPU, et trop de risque de régression sur un système durci. Voir mémoire
> `hifi_nowplaying_cpu.md`.

### 7.5 Veille, nuit & écran
- **Veille (`STANDBY_SECS = 7200`, soit 2 h)** : sans tactile / lecture / connexion
  AirPlay·BT, le rétroéclairage passe **OFF** (`brightness=0`, écriture directe sysfs,
  groupe `video`). Réveil instantané au moindre tactile / nouvelle connexion / lecture.
  Le handler SIGTERM/SIGINT restaure le rétroéclairage (un stop en veille ne laisse
  jamais l'écran noir).
- **Atténuation nocturne** : voile noir alpha 110 entre **22 h et 7 h**.
- Horloge **numérique** au repos (exigence : garder le format numérique) + carte météo.
- Locale `fr_FR.UTF-8` (générée sur l'hôte ; un `setlocale` en code est inopérant sinon).

---

## 8. Services systemd

### Actifs (vérifié)
`nowplaying` · `shairport-sync` · `NetworkManager` · `wpa_supplicant` · `avahi-daemon`
· `wifi-watchdog` · `ssh` · `dbus` · `polkit` · `udisks` · `cron` · `systemd-timesyncd`.
`bluetooth` + `bt-*` sont **inactifs** (démarrés à la demande par `setmode.sh`).
`systemctl --failed` = **vide**.

### Masqués (slimming — réversible par `unmask` + reboot)
| Catégorie | Unités masquées | Gain |
|---|---|---|
| **Samba / réseau Windows** | `smbd nmbd winbind samba-ad-dc` | **~90 MB** (le plus gros ; la Pi n'apparaît plus comme partage sur le LAN) |
| **MPD** | `mpd mpd.socket mpd2cdspvolume` | ~40 MB (inutilisé) |
| **UI web moOde** | `nginx php8.4-fpm` | config via SSH |
| **NFS / RPC** | `nfs-blkmap nfs-common rpcbind(.socket) portmap` | — |
| **Kiosque écran** | **`localdisplay.service`** | ⚠ **NE JAMAIS dé-masquer** |
| **Boot** | `NetworkManager-wait-online` · `cloud-init*` · `getty@tty1` · `apt-daily*.timer` · `hwclock` · `cryptdisks*` · `x11-common` · `alsa-utils` | boot 45→21 s |

> ⚠ **`localdisplay.service` (kiosque Xorg+chromium de moOde) doit rester masqué.**
> `nowplaying.service` déclare `Conflicts=localdisplay.service` : si localdisplay
> démarre, systemd **tue** nowplaying (SIGKILL) → l'écran tombe sur une console. Il se
> battait aussi pour `card0` (stall ~79 s au boot). En cas de retour console : vérifier
> `systemctl is-enabled localdisplay` et `fuser -v /dev/dri/card0`.
>
> **Gotcha moOde :** `mpd` et `mountmon.php` sont lancés par **`worker.php`**, pas par
> systemd (`ppid=1`). `disable` ne suffit pas → il faut **`mask`** (pour que tout
> `start` du worker échoue) **et** `pkill` l'instance.

---

## 9. Durcissement & optimisations (récapitulatif)

| Sujet | Acquis |
|---|---|
| **RAM / boot** | masquage Samba/MPD/web/NFS/cloud-init → dispo 206→**253 MB**, swap qui draine, boot **45→21 s** |
| **Écran stable** | `localdisplay` masqué (anti conflict-kill) ; sélection DSI par taille (anti double-connecteur) |
| **Audio HQ** | HDMI/IEC958 44.1 natif bit-perfect ; anti-saccade 0.5 s ; volume soft iPhone conservé |
| **CPU UI** | boucle événementielle + surfaces cachées + **glowcache** → ~2-3 % |
| **WiFi** | power-save off persistant ; watchdog 45 min + garde BT ; profil Limited prioritaire |
| **Bascule BT/AirPlay** | `timeout`+`flock`+self-heal+fallback ; reconnexion auto (trust) ; volume BT épinglé |

---

## 10. Carte des fichiers & emplacements

**Sur la Pi :**
```
/opt/nowplaying/
  ├─ nowplaying.py        ← l'app (root:root 775, exécutée par hifi)
  ├─ npconfig.py          ← config/layout
  ├─ npstate.py           ← State partagé
  ├─ setmode.sh           ← bascule AirPlay/BT
  └─ *.bak.<tag>          ← 29 sauvegardes horodatées (rollback)
/etc/systemd/system/      nowplaying.service, bt-agent/bt-bluealsa/bt-aplay.service
                          nowplaying.service.d/10-nowait-net.conf
/etc/alsa/conf.d/_audioout.conf      ← routage HDMI
/etc/shairport-sync.conf             ← conf AirPlay (+ base SQLite cfg_airplay)
/usr/local/bin/wifi-watchdog.sh      ← watchdog (service wifi-watchdog)
/lib/firmware/edid/hifi-hdmi.bin     ← EDID audio sur mesure
/boot/firmware/cmdline.txt|config.txt ← forçage HDMI, overlays
/boot/firmware/wifi-watchdog.log     ← log lisible depuis Windows
/sys/class/backlight/10-0045/brightness  ← rétroéclairage (groupe video)
/run/nowplaying-mode                 ← mode courant (airplay|bluetooth)
```

**Côté Windows (`C:\Users\geppe\Downloads\hifi\`) — classé par usage :**
- `ARCHITECTURE.md` (ce doc) + `RESUME-pi3a-volumio.md` — documentation à la racine.
- `app\` — **sources de déploiement** : `nowplaying_pi.py`, `npconfig.py`, `npstate.py`.
- `deploy\` — scripts de déploiement paramiko (`ssh_deploy_split.py`, `deploy_glowcache.py`, `ssh_deploy.py`, … + historiques one-shot).
- `diagnostics\` — sondes & mesures (`probe_pi.py`, `arch_probe.py`, `peak_probe.py`, `measure_threads.py`, captures d'écran…).
- `engine\` — moteur dirty-rect prototypé sur l'étagère (`ui_engine.py`, `test_engine.py`, `run_engine_test.py`).
- `setup\` — config hôte : firstrun, EDID, locale, WiFi.
- `deployed-units\` — unités systemd + `setmode.sh` + `nowplaying.py` + snapshot `_pi_deployed\`.
- `archive\` — versions remplacées (référence).
- Mémoire projet (externe) : `…\.claude\…\memory\hifi_*.md` (7 notes + index).

---

## 11. Procédures opérationnelles

**Déploiement de l'app (discipline « ne jamais crasher l'écran ») :**
1. éditer les sources sous `hifi\app\` ;
2. `py_compile` + **smoke-test import en `SDL_VIDEODRIVER=dummy`** sur la Pi ;
3. **backup** `nowplaying.py.bak.<tag>` ;
4. installer (`chown root:root`, `chmod 775`), `systemctl restart nowplaying` ;
5. vérifier `is-active` = active, `journalctl` sans traceback, `fuser` = seul sur card0 ;
6. **toujours relire le fichier déployé** (grep des éditions) — un `/tmp` périmé a déjà
   été livré en silence. Déployer **synchroniquement** (`exec_command`), pas via poll cd.txt.

**Bascule de source :** `sudo /opt/nowplaying/setmode.sh bluetooth|airplay|status`.

**Dépannage rapide :**
| Symptôme | Première vérif |
|---|---|
| Écran sur console | `systemctl is-enabled localdisplay` (doit être *masked*) ; `fuser -v /dev/dri/card0` |
| Pas d'audio HDMI | `cat /proc/asound/card1/pcm0p/sub0/hw_params` (≠ `closed` = ça joue) ; connecteur `connected` |
| WiFi HS après coupure | **power-cycle la box Proximus** d'abord ; lire `/boot/firmware/wifi-watchdog.log` |
| BT figé | `setmode.sh airplay` (self-heal) ; vérifier `rfkill` ; au pire reboot |
| AirPlay saccade | remonter `audio_backend_buffer_desired_length_in_seconds` vers 0.3-0.5 |

---

## 12. Limites connues & pistes

- **Latence AirPlay ~2 s** inhérente (AirPlay 1) — non réductible. BT = ~150-200 ms.
- **iPhone en Bluetooth = SBC** (pas d'AAC côté bluez-alsa) — plafond codec.
- **Volume BT en UI** : les puces volume BT visent `amixer -c Headphones` (carte jack
  **inutilisée**) → n'agissent pas sur le HDMI ; le volume AVRCP du téléphone (soft
  bluealsa) reste maître. Piste : pointer vers le volume soft bluealsa.
- **Pause initiée sur le téléphone** : reflétée en ~15 s (limite plateforme AirPlay 1,
  DACP bloqué sur cet iPhone) ; pause/play **depuis l'écran** = instantané (toggle optimiste).
- **Bail DHCP dynamique** : `.12` peut changer après reboot box → **réservation DHCP**
  recommandée (`B8:27:EB:72:EC:6D`).
- **WiFi 2.4 GHz fragile** (box qui wedge) → **USB→Ethernet** = correctif durable.

---

## 13. Aide-mémoire commandes

```bash
# état général
systemctl is-active nowplaying shairport-sync ; systemctl --failed
cat /run/nowplaying-mode
cat /proc/asound/card1/pcm0p/sub0/hw_params      # ≠ closed = audio en cours

# qui tient l'écran
sudo fuser -v /dev/dri/card0

# bascule source
sudo /opt/nowplaying/setmode.sh {airplay|bluetooth|status}

# logs
journalctl -u nowplaying -n 50 --no-pager
tail /boot/firmware/wifi-watchdog.log

# CPU par thread du rendu (depuis Downloads, via SSH)
python3 /tmp/measure_threads.py $(systemctl show -p MainPID --value nowplaying)
```

---
*Généré à partir de l'état live de la Pi (2026-06-06) et des notes projet `hifi_*.md`.*
