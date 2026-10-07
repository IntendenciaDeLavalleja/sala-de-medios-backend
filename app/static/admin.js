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
  const release = () => { urls.forEach(url => URL.revokeObjectURL(url)); urls = []; };
  window.addEventListener('pagehide', release);
  input.addEventListener('change', () => {
    release(); grid.replaceChildren(); status.textContent = ''; submit.disabled = true;
    const files = [...input.files];
    if (!files.length) return;
    if (files.length > 30 || files.some(file => file.size > Number(form.dataset.maxFile)) || files.reduce((sum, f) => sum + f.size, 0) > Number(form.dataset.maxTotal)) {
      status.textContent = 'La selección supera los límites de carga. Elegí un lote más pequeño.'; return;
    }
    if (files.some(file => !['image/jpeg', 'image/png', 'image/webp'].includes(file.type))) {
      status.textContent = 'Solo se admiten JPEG, PNG y WebP.'; return;
    }
    files.forEach(file => {
      const item = document.createElement('article'); item.className = 'preview-item';
      const img = document.createElement('img'); img.src = URL.createObjectURL(file); urls.push(img.src); img.alt = file.name;
      const caption = document.createElement('p'); caption.className = 'small muted'; caption.textContent = `${file.name} · ${(file.size / 1024**2).toFixed(1)} MB`;
      const label = document.createElement('label'); label.textContent = 'Texto alternativo';
      const alt = document.createElement('input'); alt.name = 'alt'; alt.maxLength = 500; alt.required = true; alt.value = file.name.replace(/\.[^.]+$/, '').replace(/[_-]/g, ' ');
      label.append(alt); item.append(img, caption, label); grid.append(item);
    });
    status.textContent = `${files.length} fotografías listas para subir.`; submit.disabled = false;
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
      if (progress.value === 100) status.textContent = 'Guardando originales y vistas previas en MinIO…';
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
