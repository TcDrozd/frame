// State
let photos = [];
let allPhotos = [];
let selectedPhotos = new Set();

// DOM
const gallery = document.getElementById('gallery');
const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const searchInput = document.getElementById('searchInput');
const refreshBtn = document.getElementById('refreshBtn');
const deleteBtn = document.getElementById('deleteBtn');
const photoCount = document.getElementById('photoCount');
const messageArea = document.getElementById('messageArea');
const modal = document.getElementById('modal');
const modalImage = document.getElementById('modalImage');
const modalClose = document.getElementById('modalClose');

// Initialize
loadPhotos();

// Events
uploadZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', (e) => handleFiles(e.target.files));
searchInput.addEventListener('input', handleSearch);
refreshBtn.addEventListener('click', loadPhotos);
deleteBtn.addEventListener('click', handleDelete);

modalClose.addEventListener('click', () => modal.classList.remove('active'));
modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.remove('active'); });

// Drag & drop
uploadZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadZone.classList.add('drag-over');
});
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('drag-over'));
uploadZone.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  handleFiles(e.dataTransfer.files);
});

async function loadPhotos() {
  try {
    showMessage('Loading photos...', 'info');

    const resp = await fetch('/api/photos');
    const data = await resp.json();

    if (!resp.ok || !data.ok) throw new Error(data.error || 'Failed to load photos');

    allPhotos = data.items || [];
    photos = [...allPhotos];

    selectedPhotos.clear();
    deleteBtn.disabled = true;

    renderGallery();
    updatePhotoCount();
    clearMessageSoon();
  } catch (err) {
    showMessage('Error loading photos: ' + err.message, 'error');
  }
}

function renderGallery() {
  gallery.innerHTML = '';

  if (photos.length === 0) {
    gallery.innerHTML = '<div class="loading">No photos found</div>';
    return;
  }

  photos.forEach(photo => {
    const card = document.createElement('div');
    card.className = 'photo-card';
    card.dataset.key = photo.key;

    card.innerHTML = `
      <img src="${photo.url}" alt="${escapeHtml(photo.name)}" loading="lazy">
      <div class="photo-info">
        <div class="photo-name">${escapeHtml(photo.name)}</div>
        <div class="photo-size">${photo.size_human}</div>
      </div>
    `;

    card.addEventListener('click', (e) => {
      if (e.ctrlKey || e.metaKey) {
        toggleSelection(card, photo.key);
      } else {
        showFullImage(photo.url);
      }
    });

    gallery.appendChild(card);
  });
}

function toggleSelection(card, key) {
  if (selectedPhotos.has(key)) {
    selectedPhotos.delete(key);
    card.classList.remove('selected');
  } else {
    selectedPhotos.add(key);
    card.classList.add('selected');
  }
  deleteBtn.disabled = selectedPhotos.size === 0;
}

function showFullImage(url) {
  modalImage.src = url;
  modal.classList.add('active');
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  try {
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));

    showMessage(`Uploading ${files.length} file(s)...`, 'info');

    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    const data = await resp.json();

    if (!resp.ok || !data.ok) throw new Error(data.error || 'Upload failed');

    const errors = (data.results || []).filter(r => r.status !== 'ok');
    if (errors.length) {
      showMessage(`Uploaded with ${errors.length} error(s). Check console.`, 'error');
      console.error('Upload errors:', errors);
    } else {
      showMessage(`✓ Uploaded ${files.length} file(s)`, 'success');
    }

    fileInput.value = '';
    await loadPhotos();
  } catch (err) {
    showMessage('Error uploading: ' + err.message, 'error');
  }
}

async function handleDelete() {
  if (selectedPhotos.size === 0) return;

  const confirmed = confirm(`Delete ${selectedPhotos.size} photo(s)?`);
  if (!confirmed) return;

  try {
    showMessage('Deleting photos...', 'info');

    const keys = Array.from(selectedPhotos);
    const resp = await fetch('/api/photos', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys })
    });

    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || 'Delete failed');

    if ((data.errors || []).length) {
      showMessage(`Deleted with some errors. Check console.`, 'error');
      console.error('Delete errors:', data.errors);
    } else {
      showMessage(`✓ Deleted ${keys.length} photo(s)`, 'success');
    }

    selectedPhotos.clear();
    deleteBtn.disabled = true;
    await loadPhotos();
  } catch (err) {
    showMessage('Error deleting photos: ' + err.message, 'error');
  }
}

function handleSearch() {
  const term = (searchInput.value || '').toLowerCase();
  if (!term) {
    photos = [...allPhotos];
  } else {
    photos = allPhotos.filter(p => (p.name || '').toLowerCase().includes(term));
  }
  renderGallery();
  updatePhotoCount();
}

function updatePhotoCount() {
  photoCount.textContent = `${photos.length} photo(s) in stream`;
}

function showMessage(text, type) {
  const className = type === 'error' ? 'error' : type === 'success' ? 'success' : 'loading';
  messageArea.innerHTML = `<div class="${className}">${escapeHtml(text)}</div>`;
}

function clearMessageSoon() {
  setTimeout(() => { messageArea.innerHTML = ''; }, 2000);
}

function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}
