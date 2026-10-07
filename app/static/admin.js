/* Local previews never replace verification of the object returned by MinIO. */
document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

const form = document.querySelector('#upload-form');
if (form) {
  const input = document.querySelector('#photo-files');
  const grid = document.querySelector('#before-preview');
  const submit = document.querySelector('#upload-button');
  const clear = document.querySelector('#clear-files');
  const status = document.querySelector('#upload-status');
  const progress = document.querySelector('#upload-progress');
  let urls = [];
  let previewRequests = [];
  let selectionVersion = 0;
  const release = () => {
    selectionVersion += 1;
    previewRequests.forEach(controller => controller.abort()); previewRequests = [];
    urls.forEach(url => URL.revokeObjectURL(url)); urls = [];
  };
  window.addEventListener('pagehide', release);
  input.addEventListener('change', () => {
    release(); grid.replaceChildren(); status.textContent = ''; submit.disabled = true;
    const files = [...input.files];
    if (!files.length) return;
    if (files.length > 30 || files.some(file => file.size > Number(form.dataset.maxFile)) || files.reduce((sum, f) => sum + f.size, 0) > Number(form.dataset.maxTotal)) {
      status.textContent = 'La selección supera los límites de carga. Elegí un lote más pequeño.'; return;
    }
    const extensions = /\.(jpe?g|jpe|png|webp|heic|heif|hif|avif|tiff?|bmp|dib|gif|jp2|j2k|jpf|jpx|tga|ico)$/i;
    const supportedTypes = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif', 'image/avif', 'image/tiff', 'image/bmp', 'image/x-ms-bmp', 'image/gif', 'image/jp2', 'image/x-tga', 'image/x-icon', 'image/vnd.microsoft.icon'];
    if (files.some(file => !supportedTypes.includes(file.type) && !extensions.test(file.name))) {
      status.textContent = 'Elegí imágenes JPEG, PNG, WebP, HEIC/HEIF, AVIF, TIFF, BMP o GIF. Se guardarán convertidas a WebP.'; return;
    }
    const version = selectionVersion;
    let pending = files.length;
    let failed = false;
    status.textContent = 'Preparando previsualizaciones…';
    const ready = ok => {
      if (version !== selectionVersion) return;
      pending -= 1; failed ||= !ok;
      if (pending === 0) {
        submit.disabled = failed;
        status.textContent = failed ? 'Hay archivos que no pudimos previsualizar. Revisá los mensajes y cambiá la selección.' : `${files.length} fotografías listas. Se convertirán automáticamente a WebP al subirlas.`;
      }
    };
    files.forEach(file => {
      const item = document.createElement('article'); item.className = 'preview-item';
      const img = document.createElement('img'); img.src = URL.createObjectURL(file); urls.push(img.src); img.alt = file.name;
      const caption = document.createElement('p'); caption.className = 'small muted'; caption.textContent = `${file.name} · ${(file.size / 1024**2).toFixed(1)} MB`;
      const feedback = document.createElement('p'); feedback.className = 'small muted';
      img.addEventListener('load', () => { feedback.textContent = 'Vista previa lista.'; ready(true); }, { once: true });
      img.addEventListener('error', async () => {
        if (version !== selectionVersion) return;
        feedback.textContent = 'Generando vista previa WebP…';
        const controller = new AbortController(); previewRequests.push(controller);
        const previewData = new FormData();
        previewData.append('photo', file);
        previewData.append('csrf_token', form.querySelector('[name="csrf_token"]').value);
        try {
          const response = await fetch(form.dataset.previewUrl, { method: 'POST', body: previewData, credentials: 'same-origin', signal: controller.signal });
          if (!response.ok || !response.headers.get('content-type')?.startsWith('image/webp')) {
            let message = 'No pudimos previsualizar esta imagen. Recargá la página y verificá el formato.';
            if (response.headers.get('content-type')?.includes('application/json')) message = (await response.json()).error || message;
            throw new Error(message);
          }
          const preview = await response.blob();
          if (version !== selectionVersion) return;
          img.src = URL.createObjectURL(preview); urls.push(img.src);
        } catch (error) {
          if (version !== selectionVersion || error.name === 'AbortError') return;
          feedback.textContent = error.message; ready(false);
        }
      }, { once: true });
      const label = document.createElement('label'); label.textContent = 'Texto alternativo';
      const alt = document.createElement('input'); alt.name = 'alt'; alt.maxLength = 500; alt.required = true; alt.value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ');
      label.append(alt); item.append(img, caption, feedback, label); grid.append(item);
    });
  });
  clear.addEventListener('click', () => { input.value = ''; input.dispatchEvent(new Event('change')); progress.hidden = true; });
  form.addEventListener('submit', event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const payload = new FormData(form);
    const xhr = new XMLHttpRequest(); xhr.open('POST', form.action); xhr.responseType = 'json';
    submit.disabled = true; clear.disabled = true; input.disabled = true; progress.hidden = false;
    status.textContent = 'Enviando fotografías…';
    xhr.upload.addEventListener('progress', event => {
      if (event.lengthComputable) progress.value = Math.round(event.loaded / event.total * 100);
      if (progress.value === 100) status.textContent = 'Convirtiendo a WebP y guardando fotografías y vistas previas en MinIO…';
    });
    const failed = message => { status.textContent = message; submit.disabled = false; clear.disabled = false; input.disabled = false; };
    xhr.addEventListener('load', () => {
      if (xhr.status === 201 && xhr.response?.redirect) { release(); window.location.assign(xhr.response.redirect + '#gallery'); }
      else failed(xhr.response?.error || 'No se pudo completar la carga. Recargá la página para comprobar la sesión y reintentar.');
    });
    xhr.addEventListener('error', () => failed('Se perdió la conexión. Revisá la galería antes de reintentar la carga.'));
    xhr.send(payload);
  });
}
