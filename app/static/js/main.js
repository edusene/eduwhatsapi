/* MetaBridge — scripts do painel */
(function () {
  // Confirmação em ações destrutivas
  document.querySelectorAll('[data-confirm]').forEach(function (btn) {
    btn.closest('form').addEventListener('submit', function (e) {
      if (!confirm(btn.getAttribute('data-confirm'))) {
        e.preventDefault();
      }
    });
  });

  // Copiar para área de transferência
  document.querySelectorAll('[data-clipboard-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = document.querySelector(btn.getAttribute('data-clipboard-target'));
      if (!target) return;
      var text = target.innerText;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(text).then(function () {
          btn.textContent = 'Copiado!';
          setTimeout(function () { btn.textContent = 'Copiar'; }, 2000);
        });
      } else {
        var ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        btn.textContent = 'Copiado!';
        setTimeout(function () { btn.textContent = 'Copiar'; }, 2000);
      }
    });
  });

  // Menu mobile
  var toggle = document.getElementById('menuToggle');
  var sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', function () {
      sidebar.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(sidebar.classList.contains('open')));
    });
  }

  // Feedback ao enviar form
  document.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () {
      var btn = form.querySelector('button[type="submit"]');
      if (btn) {
        btn.disabled = true;
        btn.dataset.original = btn.textContent;
        btn.textContent = 'Enviando…';
      }
    });
  });
})();
