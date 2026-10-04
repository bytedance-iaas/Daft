/* ===== page chrome shared by the visualizer mockups ===== */
(function () {
  const t = document.createElement('div'); t.className = 'toast'; document.body.append(t); let timer;
  window.toast = function (msg) { t.textContent = msg; t.classList.add('show'); clearTimeout(timer); timer = setTimeout(() => t.classList.remove('show'), 2400); };
  document.addEventListener('click', (e) => { const n = e.target.closest('[data-toast]'); if (n) { e.preventDefault(); window.toast(n.getAttribute('data-toast')); } });
  const note = document.querySelector('.mockup-note'); if (note) note.addEventListener('click', () => note.remove());
})();
