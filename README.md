# AirDrop Web

Envía fotos, videos y documentos del iPhone a esta PC por el WiFi, sin instalar nada en el teléfono.
Una página web servida desde la PC + (opcional) un Atajo de iOS para el menú **Compartir**.

- Sin dependencias: Python estándar (`server.py`) y HTML/CSS/JS sin frameworks (`web/`).
- Archivos directo a disco por streaming (no se cargan en memoria), sin límite de tamaño.
- Protegido con un token en la URL: otros en tu red no pueden enviarte nada sin él.
- Notificación en el escritorio al recibir; clic en **Abrir** abre el archivo.
- Se guardan en `~/Downloads/AirDrop` (sin pisar nada: `foto.jpg`, `foto (2).jpg`, …).

## Instalar (arranca solo al iniciar sesión)

```sh
./install.sh
```

Muestra las URLs para el iPhone. Para verlas de nuevo: `python3 server.py url`.

## En el iPhone

1. Abre la URL en **Safari** → **Compartir → Agregar a pantalla de inicio**. Queda como app: tócala, elige fotos o archivos y se envían solos.
2. Para enviar desde cualquier app (**Compartir → Enviar a PC**), crea el Atajo. En la página, el enlace *Enviar desde el menú Compartir* tiene los pasos y un botón para copiar la URL:
   - Atajos → **+** → nombre **Enviar a PC** → **ⓘ** → **Detalles** → *Mostrar en la hoja de compartir* → **Listo**.
   - Acción **Repetir con cada** sobre *Entrada del atajo*.
   - Dentro: **Obtener contenido de URL** → URL `http://…/TOKEN/upload`, método **POST**, cuerpo **Formulario**, campo **Archivo** `file` = *Elemento repetido*.

## Dirección fija (recomendado)

La IP de la PC puede cambiar. Con avahi el iPhone la encuentra por nombre (`http://pollofeliz.local:8765/…`):

```sh
sudo systemctl enable --now avahi-daemon
```

## Comandos

```sh
systemctl --user status airdrop-web        # estado
journalctl --user -u airdrop-web -f        # ver lo que llega
python3 server.py new-token                # invalidar el enlace actual
systemctl --user disable --now airdrop-web # desactivar
```

Variables (en el servicio con `systemctl --user edit airdrop-web`): `AIRDROP_PORT` (8765),
`AIRDROP_DIR` (`~/Downloads/AirDrop`), `AIRDROP_NOTIFY=0` para no notificar.

## API

`POST /TOKEN/upload` acepta `multipart/form-data` (uno o varios archivos) o el archivo en crudo
con el nombre en la cabecera `X-Filename` (URL-encoded) o en `?name=`. Si llega sin extensión,
se deduce del contenido (jpg, png, heic, mov, mp4, pdf, …).

```sh
curl -F file=@foto.jpg http://pollofeliz.local:8765/TOKEN/upload
```
