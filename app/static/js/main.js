/* AutoZap — comportamento geral das páginas (sem dependências externas). */
(function () {
  'use strict';

  // Utilitário global: requisições JSON com CSRF (usado por outros scripts).
  window.AZ = window.AZ || {};
  AZ.csrf = document.body.getAttribute('data-csrf') || '';
  AZ.json = function (url, options) {
    options = options || {};
    var headers = { 'Accept': 'application/json', 'X-CSRF-Token': AZ.csrf };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    return fetch(url, {
      method: options.method || 'GET',
      credentials: 'same-origin',
      headers: headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) {
          var message = (data && (data.message || (data.error && data.error.message))) || 'Erro ' + resp.status;
          var err = new Error(message);
          err.status = resp.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  };
  AZ.toast = function (message, kind) {
    var box = document.querySelector('.toasts');
    if (!box) {
      box = document.createElement('div');
      box.className = 'toasts';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    var el = document.createElement('div');
    el.className = 'toast toast-' + (kind || 'info');
    var text = document.createElement('div');
    text.textContent = message;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Fechar aviso');
    close.textContent = '×';
    el.appendChild(text);
    el.appendChild(close);
    box.appendChild(el);
    autoDismiss(el);
  };

  function autoDismiss(el) {
    var isError = el.classList.contains('toast-error');
    setTimeout(function () { el.remove(); }, isError ? 9000 : 5000);
  }
  document.querySelectorAll('.toast').forEach(autoDismiss);
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.toast-close');
    if (btn) btn.closest('.toast').remove();
  });

  // Diálogos: [data-open="id"] abre; [data-close] fecha; clique no fundo fecha.
  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-open]');
    if (opener) {
      var dlg = document.getElementById(opener.getAttribute('data-open'));
      if (dlg && typeof dlg.showModal === 'function') {
        var parent = opener.closest('dialog[open]');
        if (parent) parent.close();
        dlg.showModal();
        var first = dlg.querySelector('input:not([type=hidden]), select, textarea');
        if (first) first.focus();
      }
      return;
    }
    var closer = e.target.closest('[data-close]');
    if (closer) {
      var d = closer.closest('dialog');
      if (d) d.close();
      return;
    }
    if (e.target.tagName === 'DIALOG' && e.target.open) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });

  // Confirmação de ações destrutivas.
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!window.confirm(form.getAttribute('data-confirm'))) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
    });
  });

  // Estado de carregamento nos envios de formulário (evita clique duplo).
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.hasAttribute('data-no-loading')) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) {
      setTimeout(function () { btn.disabled = true; btn.classList.add('is-loading'); }, 0);
    }
  });
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('.is-loading').forEach(function (b) { b.disabled = false; b.classList.remove('is-loading'); });
  });

  // Selects que enviam o formulário ao mudar.
  document.querySelectorAll('select[data-autosubmit]').forEach(function (sel) {
    sel.addEventListener('change', function () { sel.form.submit(); });
  });

  // Copiar para a área de transferência.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-copy]');
    if (!btn) return;
    var target = document.querySelector(btn.getAttribute('data-copy'));
    if (!target) return;
    var text = target.textContent.trim();
    var done = function () { AZ.toast('Copiado para a área de transferência.', 'success'); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  });
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (err) { /* sem suporte */ }
    ta.remove();
  }

  // Menu lateral no celular.
  var toggle = document.getElementById('menuToggle');
  var sidebar = document.getElementById('sidebar');
  var scrim = document.getElementById('scrim');
  function setMenu(open) {
    if (!sidebar) return;
    sidebar.classList.toggle('open', open);
    if (scrim) scrim.classList.toggle('open', open);
    if (toggle) toggle.setAttribute('aria-expanded', String(open));
  }
  if (toggle) toggle.addEventListener('click', function () { setMenu(!sidebar.classList.contains('open')); });
  if (scrim) scrim.addEventListener('click', function () { setMenu(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setMenu(false); });

  // Botão "voltar".
  document.querySelectorAll('[data-history-back]').forEach(function (b) {
    b.addEventListener('click', function () { history.back(); });
  });

  // Alternância texto/modelo no envio de teste.
  document.querySelectorAll('select[data-toggle-kind]').forEach(function (sel) {
    var form = sel.form;
    function apply() {
      form.querySelectorAll('[data-kind]').forEach(function (block) {
        block.hidden = block.getAttribute('data-kind') !== sel.value;
      });
    }
    sel.addEventListener('change', apply);
    apply();
  });
  document.querySelectorAll('select[data-template-select]').forEach(function (sel) {
    sel.addEventListener('change', function () {
      var opt = sel.options[sel.selectedIndex];
      var lang = sel.form.querySelector('[name="template_language"]');
      if (opt && lang && opt.getAttribute('data-language')) lang.value = opt.getAttribute('data-language');
    });
  });

  // Tooltip do gráfico de barras (hover e foco por teclado).
  document.querySelectorAll('[data-chart]').forEach(function (chart) {
    var area = chart.querySelector('.chart-area');
    var tip = document.createElement('div');
    tip.className = 'chart-tip';
    tip.hidden = true;
    area.appendChild(tip);
    function show(col) {
      tip.innerHTML = '';
      var title = document.createElement('b');
      title.textContent = col.getAttribute('data-date');
      tip.appendChild(title);
      [['Recebidas', 'data-in', 'var(--series-1)'], ['Enviadas', 'data-out', 'var(--series-2)']].forEach(function (row) {
        var line = document.createElement('div');
        var sw = document.createElement('i');
        sw.style.background = row[2];
        var label = document.createElement('span');
        label.textContent = row[0];
        var val = document.createElement('strong');
        val.textContent = Number(col.getAttribute(row[1])).toLocaleString('pt-BR');
        line.appendChild(sw); line.appendChild(label); line.appendChild(val);
        tip.appendChild(line);
      });
      var a = area.getBoundingClientRect();
      var c = col.getBoundingClientRect();
      var x = c.left - a.left + c.width / 2;
      x = Math.max(70, Math.min(a.width - 70, x));
      tip.style.left = x + 'px';
      tip.style.top = '28px';
      tip.hidden = false;
    }
    chart.querySelectorAll('.chart-col').forEach(function (col) {
      col.addEventListener('mouseenter', function () { show(col); });
      col.addEventListener('focus', function () { show(col); });
      col.addEventListener('mouseleave', function () { tip.hidden = true; });
      col.addEventListener('blur', function () { tip.hidden = true; });
    });
  });
})();
