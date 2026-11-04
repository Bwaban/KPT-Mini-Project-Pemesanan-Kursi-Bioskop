const seatMap = document.querySelector('#seat-map');
const selectedSeatLabel = document.querySelector('#selected-seat');
const bookButton = document.querySelector('#book-button');
const nameInput = document.querySelector('#customer-name');
const connection = document.querySelector('#connection');
const bookingList = document.querySelector('#booking-list');
const bookingCount = document.querySelector('#booking-count');
const toast = document.querySelector('#toast');

let seats = [];
const selectedSeats = new Set();
let bookingInProgress = false;
let toastTimer;
const bookingsKey = 'bioskop-kita-bookings';

function localBookings() {
  try { return JSON.parse(localStorage.getItem(bookingsKey) || '[]'); }
  catch { return []; }
}

function saveBookings(bookings) {
  localStorage.setItem(bookingsKey, JSON.stringify(bookings));
  renderBookings();
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 3200);
}

function renderSeats() {
  seatMap.replaceChildren();
  const rows = [...new Set(seats.map(seat => seat.baris))];
  const heading = document.createElement('div');
  heading.className = 'seat-row seat-heading';
  heading.append(document.createElement('span'));
  for (let number = 1; number <= 10; number++) {
    if (number === 6) {
      const aisle = document.createElement('span');
      aisle.className = 'aisle-space';
      heading.append(aisle);
    }
    const label = document.createElement('span');
    label.className = 'column-label';
    label.textContent = number;
    heading.append(label);
  }
  seatMap.append(heading);
  for (const row of rows) {
    const rowElement = document.createElement('div');
    rowElement.className = 'seat-row';
    const rowLabel = document.createElement('span');
    rowLabel.className = 'row-label';
    rowLabel.textContent = row;
    rowElement.append(rowLabel);
    for (const seat of seats.filter(item => item.baris === row).sort((a, b) => a.nomor - b.nomor)) {
      if (seat.nomor === 6) {
        const aisle = document.createElement('span');
        aisle.className = 'aisle-space';
        aisle.setAttribute('aria-hidden', 'true');
        rowElement.append(aisle);
      }
      const button = document.createElement('button');
      button.className = `seat${seat.tersedia ? '' : ' booked'}${selectedSeats.has(seat.id) ? ' selected' : ''}`;
      button.textContent = seat.nomor;
      button.title = `${seat.id} — ${seat.tersedia ? 'tersedia' : 'terpesan'}`;
      button.setAttribute('aria-label', `Kursi ${seat.id}, ${seat.tersedia ? 'tersedia' : 'terpesan'}`);
      button.disabled = !seat.tersedia;
      button.addEventListener('click', () => {
        if (selectedSeats.has(seat.id)) selectedSeats.delete(seat.id);
        else selectedSeats.add(seat.id);
        renderSeats();
        updateSelection();
      });
      rowElement.append(button);
    }
    seatMap.append(rowElement);
  }
  updateSelection();
}

function updateSelection() {
  const selectedIds = seats.filter(seat => selectedSeats.has(seat.id)).map(seat => seat.id);
  const count = selectedIds.length;
  selectedSeatLabel.textContent = count ? selectedIds.join(', ') : 'Belum ada';
  document.querySelector('#selected-count').textContent = count;
  document.querySelector('#ticket-total').textContent = `Rp ${new Intl.NumberFormat('id-ID').format(count * 35000)}`;
  bookButton.disabled = count === 0 || bookingInProgress;
  bookButton.innerHTML = bookingInProgress ? 'Memproses…' : count ? `Pesan ${count} kursi <span>→</span>` : 'Pesan kursi <span>→</span>';
}

function renderBookings() {
  const bookings = localBookings();
  bookingCount.textContent = `${bookings.length} tiket`;
  bookingList.replaceChildren();
  if (!bookings.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-bookings';
    empty.textContent = 'Belum ada pesanan di perangkat ini.';
    bookingList.append(empty);
    return;
  }
  for (const booking of bookings) {
    const item = document.createElement('div');
    item.className = 'booking-item';
    const label = document.createElement('span');
    label.textContent = `${booking.kursi_id} · ${booking.nama_pemesan}`;
    const cancel = document.createElement('button');
    cancel.textContent = 'Batalkan';
    cancel.addEventListener('click', () => cancelBooking(booking));
    item.append(label, cancel);
    bookingList.append(item);
  }
}

async function loadSeats() {
  try {
    const response = await fetch('/api/seats');
    if (!response.ok) throw new Error('Gagal mengambil status kursi');
    const data = await response.json();
    seats = data.kursi;
    for (const id of selectedSeats) {
      if (!seats.some(seat => seat.id === id && seat.tersedia)) selectedSeats.delete(id);
    }
    renderSeats();
  } catch (error) {
    connection.className = 'connection offline';
    connection.innerHTML = '<span></span> Server tidak terhubung';
  }
}

function connectSeatUpdates() {
  const events = new EventSource('/api/stream');
  events.addEventListener('open', () => {
    connection.className = 'connection online';
    connection.innerHTML = '<span></span> Live';
  });
  events.addEventListener('seats', event => {
    const data = JSON.parse(event.data);
    seats = data.kursi;
    for (const id of selectedSeats) {
      if (!seats.some(seat => seat.id === id && seat.tersedia)) selectedSeats.delete(id);
    }
    renderSeats();
  });
  events.addEventListener('error', () => {
    connection.className = 'connection offline';
    connection.innerHTML = '<span></span> Menyambungkan ulang…';
  });
}

async function bookSelectedSeat() {
  if (!selectedSeats.size) return;
  const nama = nameInput.value.trim();
  if (!nama) {
    nameInput.focus();
    showToast('Isi nama pemesan dulu ya.', true);
    return;
  }
  bookingInProgress = true;
  updateSelection();
  const kursiIds = seats.filter(seat => selectedSeats.has(seat.id)).map(seat => seat.id);
  bookButton.textContent = 'Memproses…';
  const payload = { request_id: crypto.randomUUID(), kursi_ids: kursiIds, nama_pemesan: nama };
  try {
    const response = await fetch('/api/book-batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Pemesanan gagal. Coba lagi.');
    if (!result.sukses) throw new Error(result.pesan);
    saveBookings([...localBookings(), ...result.pesanan.map(item => ({ ...item, nama_pemesan: nama }))]);
    selectedSeats.clear();
    showToast(result.pesan);
    await loadSeats();
  } catch (error) {
    showToast(error.message || 'Pemesanan gagal. Coba lagi.', true);
  } finally {
    bookingInProgress = false;
    updateSelection();
  }
}

async function cancelBooking(booking) {
  try {
    const response = await fetch('/api/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: crypto.randomUUID(), booking_id: booking.booking_id, nama_pemesan: booking.nama_pemesan || '' }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Pembatalan gagal.');
    if (!result.sukses) throw new Error(result.pesan);
    saveBookings(localBookings().filter(item => item.booking_id !== booking.booking_id));
    showToast(result.pesan);
    await loadSeats();
  } catch (error) {
    showToast(error.message || 'Pembatalan gagal.', true);
  }
}

bookButton.addEventListener('click', bookSelectedSeat);
nameInput.addEventListener('input', updateSelection);
renderBookings();
loadSeats();
connectSeatUpdates();
