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

  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function wait(ms) { return new Promise(function (r) { setTimeout(r, reduceMotion ? 0 : ms); }); }

  // ------------------------------------------------------------------ avisos
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
  function dismissToast(el) {
    if (!el || el.classList.contains('is-leaving')) return;
    el.classList.add('is-leaving');
    setTimeout(function () { el.remove(); }, 200);
  }
  function autoDismiss(el) {
    var isError = el.classList.contains('toast-error');
    setTimeout(function () { dismissToast(el); }, isError ? 9000 : 5000);
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.toast-close');
    if (btn) dismissToast(btn.closest('.toast'));
  });

  // ------------------------------------------------------- barra de progresso
  var NAV_KEY = 'az-nav';
  var bar = document.createElement('div');
  bar.className = 'page-progress';
  bar.setAttribute('aria-hidden', 'true');
  document.body.appendChild(bar);

  function startBar() {
    bar.classList.remove('is-done');
    void bar.offsetWidth; // reinicia a transição
    bar.classList.add('is-active');
  }
  function finishBar() {
    if (!bar.classList.contains('is-active')) return;
    bar.classList.add('is-done');
    setTimeout(function () {
      if (bar.classList.contains('is-done')) bar.classList.remove('is-active', 'is-done');
    }, 600);
  }
  // Navegação tradicional (formulários, páginas fora do painel).
  function startNav() {
    document.body.classList.add('is-navigating');
    startBar();
    try { sessionStorage.setItem(NAV_KEY, '1'); } catch (err) { /* armazenamento indisponível */ }
  }
  AZ.startNav = startNav;
  var cameFromNav = false;
  try { cameFromNav = sessionStorage.getItem(NAV_KEY) === '1'; sessionStorage.removeItem(NAV_KEY); } catch (err) { /* idem */ }
  if (cameFromNav) {
    bar.classList.add('is-active');
    requestAnimationFrame(function () { requestAnimationFrame(finishBar); });
  }
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) { document.body.classList.remove('is-navigating'); bar.classList.remove('is-active', 'is-done'); }
    document.querySelectorAll('.is-loading').forEach(function (b) { b.disabled = false; b.classList.remove('is-loading'); });
  });

  // ------------------------------------------------------------------
  // Navegação do painel sem recarregar a página: o menu lateral fica fixo,
  // o conteúdo atual sai animado enquanto o próximo é buscado em paralelo
  // e o novo entra animado. O servidor continua renderizando o HTML (mesma
  // autorização de sempre). Em qualquer falha, cai na navegação normal.
  // ------------------------------------------------------------------
  var appShell = document.querySelector('.app');
  var mainEl = document.querySelector('.app .main');
  var swapEnabled = !!(appShell && mainEl && window.fetch && window.DOMParser && history.pushState);
  var prefetchCache = {};           // url -> { promise, at }
  var PREFETCH_TTL = 15000;
  var navToken = 0;

  function eligibleLink(a) {
    if (!a || a.hasAttribute('download') || a.hasAttribute('data-no-swap')) return null;
    if (a.target && a.target !== '_self') return null;
    var url;
    try { url = new URL(a.href, window.location.href); } catch (err) { return null; }
    if (url.origin !== window.location.origin) return null;
    return url;
  }
  function isPanelUrl(url) { return url.pathname.indexOf('/dashboard') === 0; }

  function fetchPage(href) {
    var key = href.split('#')[0];
    var hit = prefetchCache[key];
    if (hit && Date.now() - hit.at < PREFETCH_TTL) { delete prefetchCache[key]; return hit.promise; }
    return fetch(key, { credentials: 'same-origin', headers: { 'Accept': 'text/html', 'X-AZ-Nav': '1' } })
      .then(function (resp) { return resp.text().then(function (html) { return { resp: resp, html: html }; }); });
  }
  function prefetch(href) {
    var key = href.split('#')[0];
    var hit = prefetchCache[key];
    if (hit && Date.now() - hit.at < PREFETCH_TTL) return;
    var promise = fetch(key, { credentials: 'same-origin', headers: { 'Accept': 'text/html', 'X-AZ-Nav': '1' } })
      .then(function (resp) { return resp.text().then(function (html) { return { resp: resp, html: html }; }); });
    promise.catch(function () { delete prefetchCache[key]; });
    prefetchCache[key] = { promise: promise, at: Date.now() };
  }

  function markActive(a) {
    var nav = a && a.closest('.nav');
    if (nav) {
      nav.querySelectorAll('a.active').forEach(function (x) { x.classList.remove('active'); x.removeAttribute('aria-current'); });
      a.classList.add('active');
      a.setAttribute('aria-current', 'page');
    }
    var tabs = a && a.closest('.tabs');
    if (tabs) {
      tabs.querySelectorAll('a.active').forEach(function (x) { x.classList.remove('active'); });
      a.classList.add('active');
    }
  }

  function hardNavigate(href) {
    startNav();
    window.location.assign(href);
  }

  function swapTo(href, options) {
    options = options || {};
    var token = ++navToken;
    var content = document.getElementById('conteudo');
    startBar();
    if (content) content.classList.add('is-leaving');
    setMenu(false);

    return Promise.all([fetchPage(href), wait(170)]).then(function (results) {
      if (token !== navToken) return; // outro clique mais recente venceu
      var resp = results[0].resp;
      var finalUrl = new URL(resp.url || href, window.location.href);
      var type = resp.headers.get('Content-Type') || '';
      if (type.indexOf('text/html') === -1 || !isPanelUrl(finalUrl)) { hardNavigate(finalUrl.href); return; }
      var doc = new DOMParser().parseFromString(results[0].html, 'text/html');
      var newMain = doc.querySelector('.app .main');
      if (!newMain || doc.querySelector('meta[name="az-full-reload"]')) { hardNavigate(finalUrl.href); return; }

      // Troca o conteúdo (mantém a barra superior do celular).
      Array.prototype.slice.call(mainEl.children).forEach(function (child) {
        if (!child.classList.contains('topbar')) child.remove();
      });
      Array.prototype.slice.call(newMain.children).forEach(function (child) {
        if (!child.classList.contains('topbar')) mainEl.appendChild(document.adoptNode(child));
      });
      // Menu lateral (estado ativo, organização) e avisos da nova página.
      var newNav = doc.querySelector('.sidebar .nav');
      var nav = document.querySelector('.sidebar .nav');
      if (newNav && nav) nav.innerHTML = newNav.innerHTML;
      var oldToasts = document.querySelector('.toasts');
      if (oldToasts) oldToasts.remove();
      var newToasts = doc.querySelector('.toasts');
      if (newToasts) document.body.appendChild(document.adoptNode(newToasts));
      document.title = doc.title;

      var target = finalUrl.href + (finalUrl.hash ? '' : (new URL(href, window.location.href).hash || ''));
      if (options.push !== false) history.pushState({ az: true }, '', target);
      else if (target !== window.location.href) history.replaceState({ az: true }, '', target);

      var hash = new URL(target).hash;
      var anchor = hash && document.getElementById(decodeURIComponent(hash.slice(1)));
      if (anchor) anchor.scrollIntoView();
      else if (options.push !== false) window.scrollTo(0, 0);

      initPage(document);
      runPageScripts(doc);
      var focusTarget = document.getElementById('conteudo');
      if (focusTarget) {
        focusTarget.setAttribute('tabindex', '-1');
        focusTarget.focus({ preventScroll: true });
      }
      finishBar();
    }).catch(function () {
      if (token === navToken) hardNavigate(href);
    });
  }

  // Executa os scripts específicos da página nova (ex.: chat).
  function runPageScripts(doc) {
    doc.querySelectorAll('body script[src]').forEach(function (old) {
      var src = old.getAttribute('src');
      if (!src || src.indexOf('/js/main.js') !== -1) return;
      var s = document.createElement('script');
      s.src = src;
      s.async = false;
      document.body.appendChild(s);
      s.addEventListener('load', function () { s.remove(); });
    });
  }

  if (swapEnabled) {
    history.replaceState({ az: true }, '', window.location.href);
    window.addEventListener('popstate', function (e) {
      if (!e.state || !e.state.az) return;
      swapTo(window.location.href, { push: false });
    });
    // Pré-carrega ao passar o mouse (intenção) nos itens de menu e abas.
    var hoverTimer = null;
    document.addEventListener('mouseover', function (e) {
      var a = e.target.closest('.nav a[href], .tabs a[href]');
      if (!a) return;
      var url = eligibleLink(a);
      if (!url || !isPanelUrl(url) || url.href.split('#')[0] === window.location.href.split('#')[0]) return;
      clearTimeout(hoverTimer);
      hoverTimer = setTimeout(function () { prefetch(url.href); }, 90);
    });
    document.addEventListener('mouseout', function (e) {
      if (e.target.closest('.nav a[href], .tabs a[href]')) clearTimeout(hoverTimer);
    });
  }

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest('a[href]');
    var url = eligibleLink(a);
    if (!url) return;
    if (url.pathname === window.location.pathname && url.search === window.location.search) return; // âncora na mesma página
    markActive(a);
    if (swapEnabled && isPanelUrl(url)) {
      e.preventDefault();
      swapTo(url.href);
      return;
    }
    startNav();
  });

  // ------------------------------------------------------------- diálogos
  // [data-open="id"] abre; [data-close] fecha; clique no fundo fecha.
  function closeDialog(d) {
    if (!d || !d.open || d.classList.contains('is-closing')) return;
    d.classList.add('is-closing');
    setTimeout(function () { d.classList.remove('is-closing'); d.close(); }, reduceMotion ? 0 : 150);
  }
  AZ.closeDialog = closeDialog;
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
      closeDialog(closer.closest('dialog'));
      return;
    }
    if (e.target.tagName === 'DIALOG' && e.target.open) {
      var r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeDialog(e.target);
    }
  });

  // -------------------------------------------------------- formulários
  // Confirmação de ações destrutivas (captura: roda antes do estado de envio).
  document.addEventListener('submit', function (e) {
    var form = e.target.closest('form[data-confirm]');
    if (form && !window.confirm(form.getAttribute('data-confirm'))) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);
  // Estado de carregamento nos envios (evita clique duplo).
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.hasAttribute('data-no-loading')) return;
    var btn = form.querySelector('button[type="submit"]');
    if (btn) {
      setTimeout(function () { btn.disabled = true; btn.classList.add('is-loading'); }, 0);
    }
    if ((form.getAttribute('method') || '').toLowerCase() !== 'dialog') startNav();
  });
  document.addEventListener('change', function (e) {
    var sel = e.target;
    if (sel.matches && sel.matches('select[data-autosubmit]')) { startNav(); sel.form.submit(); return; }
    if (sel.matches && sel.matches('select[data-toggle-kind]')) { applyKind(sel); return; }
    if (sel.matches && sel.matches('select[data-template-select]')) {
      var opt = sel.options[sel.selectedIndex];
      var lang = sel.form.querySelector('[name="template_language"]');
      if (opt && lang && opt.getAttribute('data-language')) lang.value = opt.getAttribute('data-language');
    }
  });
  function applyKind(sel) {
    sel.form.querySelectorAll('[data-kind]').forEach(function (block) {
      block.hidden = block.getAttribute('data-kind') !== sel.value;
    });
  }

  // ------------------------------------------------------------- copiar
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

  // -------------------------------------------------- menu lateral (celular)
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
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-history-back]')) history.back();
  });

  // ------------------------------------------------------------------
  // Inicialização por página (roda no carregamento e após cada troca).
  // ------------------------------------------------------------------
  function initPage(root) {
    root.querySelectorAll('.toast:not([data-init])').forEach(function (t) { t.setAttribute('data-init', '1'); autoDismiss(t); });
    root.querySelectorAll('select[data-toggle-kind]').forEach(applyKind);
    root.querySelectorAll('[data-chart]:not([data-init])').forEach(initChart);
  }

  // Tooltip do gráfico de barras (hover e foco por teclado).
  function initChart(chart) {
    chart.setAttribute('data-init', '1');
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
  }

  initPage(document);
})();
