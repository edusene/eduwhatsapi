/* Caixa de entrada (plano API + Chat).
   Atualização por polling leve (lista a cada 6s, conversa aberta a cada 3s),
   que funciona em qualquer hospedagem sem WebSocket. Todo conteúdo vindo de
   clientes é inserido com textContent (nunca innerHTML) para evitar XSS. */
(function () {
  'use strict';
  var root = document.getElementById('inbox');
  if (!root) return;
  var API = root.getAttribute('data-api');
  var MEDIA = root.getAttribute('data-media');
  var canSend = root.getAttribute('data-can-send') === 'true';
  var me = root.getAttribute('data-user');

  var els = {
    list: document.getElementById('convList'),
    search: document.getElementById('convSearch'),
    empty: document.getElementById('chatEmpty'),
    chat: document.getElementById('chat'),
    messages: document.getElementById('chatMessages'),
    name: document.getElementById('chatName'),
    sub: document.getElementById('chatSub'),
    avatar: document.getElementById('chatAvatar'),
    back: document.getElementById('chatBack'),
    composer: document.getElementById('composer'),
    text: document.getElementById('composerText'),
    windowNote: document.getElementById('windowNote'),
    assign: document.getElementById('assignBtn'),
    resolve: document.getElementById('resolveBtn')
  };

  var state = {
    filter: 'open',
    query: '',
    conversations: [],
    activeId: null,
    active: null,
    messages: {},       // id -> mensagem
    lastUpdated: null,  // maior updated_at visto (para ?after=)
    windowOpen: false,
    sending: false
  };

  // ------------------------------------------------------------------ util
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function svgIcon(paths, cls) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'icon ' + (cls || ''));
    svg.setAttribute('aria-hidden', 'true');
    paths.forEach(function (d) {
      var p = document.createElementNS(ns, 'path');
      p.setAttribute('d', d);
      svg.appendChild(p);
    });
    return svg;
  }
  var ICONS = {
    check: ['M20 6 9 17l-5-5'],
    double: ['M17 7 8.5 15.5 5 12', 'm22 7-8.5 8.5-1.2-1.2'],
    clock: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 7v5l3 2'],
    alert: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 8v5M12 16h.01'],
    file: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z', 'M14 2v6h6']
  };
  function initials(name, waId) {
    var base = (name || '').trim();
    if (!base) return (waId || '?').slice(-2);
    var parts = base.split(/\s+/);
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }
  function formatPhone(waId) {
    var d = String(waId || '');
    if (d.indexOf('55') === 0 && (d.length === 12 || d.length === 13)) {
      var local = d.slice(4);
      return '+55 ' + d.slice(2, 4) + ' ' + local.slice(0, -4) + '-' + local.slice(-4);
    }
    return d ? '+' + d : '';
  }
  function parseDate(v) { return v ? new Date(v) : null; }
  function timeLabel(v) {
    var d = parseDate(v);
    if (!d) return '';
    var now = new Date();
    if (d.toDateString() === now.toDateString()) {
      return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    }
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return 'Ontem';
    return d.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' });
  }
  function dayLabel(d) {
    var now = new Date();
    if (d.toDateString() === now.toDateString()) return 'Hoje';
    var y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return 'Ontem';
    return d.toLocaleDateString('pt-BR', { day: '2-digit', month: 'long', year: 'numeric' });
  }

  // ------------------------------------------------------------ conversas
  function loadConversations() {
    var params = new URLSearchParams();
    if (state.filter === 'mine') { params.set('mine', '1'); params.set('status', 'open'); }
    else params.set('status', state.filter);
    if (state.query) params.set('q', state.query);
    return AZ.json(API + '/conversations?' + params.toString())
      .then(function (data) {
        state.conversations = data.conversations || [];
        renderList();
        if (state.activeId) {
          var current = state.conversations.find(function (c) { return c.id === state.activeId; });
          if (current) { state.active = current; renderHeader(); }
        }
      })
      .catch(function () { /* mantém a lista atual em falhas temporárias */ });
  }

  function renderList() {
    els.list.innerHTML = '';
    if (!state.conversations.length) {
      var empty = el('div', 'empty');
      empty.appendChild(el('p', null, state.query ? 'Nenhuma conversa encontrada.' :
        (state.filter === 'resolved' ? 'Nenhuma conversa resolvida.' : 'Nenhuma conversa por aqui ainda.')));
      els.list.appendChild(empty);
      return;
    }
    state.conversations.forEach(function (c) {
      var btn = el('button', 'conv' + (c.id === state.activeId ? ' active' : ''));
      btn.type = 'button';
      btn.setAttribute('data-id', c.id);
      btn.appendChild(el('div', 'avatar', initials(c.contact.name, c.contact.wa_id)));
      var body = el('div', 'conv-body');
      var top = el('div', 'conv-top');
      top.appendChild(el('span', 'conv-name', c.contact.name || formatPhone(c.contact.wa_id)));
      top.appendChild(el('span', 'conv-time', timeLabel(c.last_message_at)));
      var bottom = el('div', 'conv-bottom');
      bottom.appendChild(el('span', 'conv-preview', c.last_message_preview || ''));
      if (c.unread_count > 0 && c.id !== state.activeId) bottom.appendChild(el('span', 'unread', String(c.unread_count)));
      body.appendChild(top);
      body.appendChild(bottom);
      btn.appendChild(body);
      btn.addEventListener('click', function () { openConversation(c.id); });
      els.list.appendChild(btn);
    });
  }

  // ------------------------------------------------------------- conversa
  function openConversation(id) {
    state.activeId = id;
    state.active = state.conversations.find(function (c) { return c.id === id; }) || null;
    state.messages = {};
    state.lastUpdated = null;
    els.messages.innerHTML = '';
    els.empty.hidden = true;
    els.chat.hidden = false;
    root.classList.add('has-active');
    if (history.replaceState) history.replaceState(null, '', '#c=' + id);
    renderHeader();
    renderList();
    loadMessages(true).then(function () {
      AZ.json(API + '/conversations/' + id + '/read' + (canSend ? '?receipt=1' : ''), { method: 'POST', body: {} })
        .catch(function () {});
    });
    if (els.text) els.text.focus();
  }

  function renderHeader() {
    var c = state.active;
    if (!c) return;
    els.name.textContent = c.contact.name || formatPhone(c.contact.wa_id);
    var via = c.channel && (c.channel.display_phone_number || c.channel.phone_number_id);
    els.sub.textContent = formatPhone(c.contact.wa_id) + (via ? ' · via ' + via : '');
    els.avatar.textContent = initials(c.contact.name, c.contact.wa_id);
    if (els.assign) {
      els.assign.textContent = c.assigned_to === me ? 'Liberar' : 'Assumir';
      els.assign.title = c.assigned_to && c.assigned_to !== me ? 'Atribuída a outro membro' : '';
    }
    if (els.resolve) els.resolve.lastChild.textContent = c.status === 'resolved' ? ' Reabrir' : ' Resolver';
  }

  function loadMessages(scrollToEnd) {
    var id = state.activeId;
    if (!id) return Promise.resolve();
    var url = API + '/conversations/' + id + '/messages';
    if (state.lastUpdated) url += '?after=' + encodeURIComponent(state.lastUpdated);
    return AZ.json(url).then(function (data) {
      if (id !== state.activeId) return;
      var nearBottom = els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 120;
      var changed = false;
      (data.messages || []).forEach(function (m) {
        state.messages[m.id] = m;
        changed = true;
        if (!state.lastUpdated || (m.updated_at && m.updated_at > state.lastUpdated)) state.lastUpdated = m.updated_at;
      });
      state.windowOpen = !!data.window_open;
      updateComposer();
      if (changed || scrollToEnd) {
        renderMessages();
        if (scrollToEnd || nearBottom) els.messages.scrollTop = els.messages.scrollHeight;
      }
    }).catch(function (err) {
      if (err.status === 404) { AZ.toast('Conversa não encontrada.', 'error'); }
    });
  }

  function updateComposer() {
    if (!els.composer) return;
    els.windowNote.hidden = state.windowOpen;
    els.text.disabled = !state.windowOpen;
    els.text.placeholder = state.windowOpen
      ? 'Digite uma mensagem (Enter envia, Shift+Enter quebra linha)'
      : 'Janela de 24h fechada — envie um modelo';
  }

  function statusIcon(m) {
    if (m.direction !== 'outbound') return null;
    if (m.status === 'failed') return svgIcon(ICONS.alert, 'failed');
    if (m.status === 'read') return svgIcon(ICONS.double, 'read');
    if (m.status === 'delivered') return svgIcon(ICONS.double);
    if (m.status === 'sent') return svgIcon(ICONS.check);
    return svgIcon(ICONS.clock);
  }

  function renderContent(m, bubble) {
    var c = m.content || {};
    var type = m.type;
    if (type === 'text') { bubble.appendChild(document.createTextNode(c.body || '')); return; }
    if (type === 'image' || type === 'sticker') {
      if (m.direction === 'inbound' && c.id) {
        var img = el('img'); img.src = MEDIA + '/' + m.id; img.alt = c.caption || 'Imagem recebida'; img.loading = 'lazy';
        bubble.appendChild(img);
      } else bubble.appendChild(el('span', 'tag', type === 'image' ? '📷 Imagem' : 'Figurinha'));
      if (c.caption) bubble.appendChild(document.createTextNode(c.caption));
      return;
    }
    if (type === 'video') {
      if (m.direction === 'inbound' && c.id) { var v = el('video'); v.controls = true; v.preload = 'none'; v.src = MEDIA + '/' + m.id; bubble.appendChild(v); }
      else bubble.appendChild(el('span', 'tag', '🎬 Vídeo'));
      if (c.caption) bubble.appendChild(document.createTextNode(c.caption));
      return;
    }
    if (type === 'audio') {
      if (m.direction === 'inbound' && c.id) { var a = el('audio'); a.controls = true; a.preload = 'none'; a.src = MEDIA + '/' + m.id; bubble.appendChild(a); }
      else bubble.appendChild(el('span', 'tag', '🎤 Áudio'));
      return;
    }
    if (type === 'document') {
      var box = el(m.direction === 'inbound' && c.id ? 'a' : 'div', 'file');
      if (box.tagName === 'A') { box.href = MEDIA + '/' + m.id; box.setAttribute('download', c.filename || 'documento'); }
      box.appendChild(svgIcon(ICONS.file));
      box.appendChild(el('span', null, c.filename || 'Documento'));
      bubble.appendChild(box);
      if (c.caption) bubble.appendChild(document.createTextNode(c.caption));
      return;
    }
    if (type === 'location') {
      bubble.appendChild(el('span', 'tag', '📍 Localização'));
      var label = [c.name, c.address].filter(Boolean).join(' — ') || (c.latitude + ', ' + c.longitude);
      var link = el('a', null, label);
      link.href = 'https://www.google.com/maps?q=' + encodeURIComponent(c.latitude + ',' + c.longitude);
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      bubble.appendChild(link);
      return;
    }
    if (type === 'template') {
      bubble.appendChild(el('span', 'tag', 'Modelo'));
      bubble.appendChild(document.createTextNode(c.name || ''));
      return;
    }
    if (type === 'interactive' || type === 'button') {
      bubble.appendChild(el('span', 'tag', m.direction === 'inbound' ? 'Resposta de botão' : 'Mensagem interativa'));
      bubble.appendChild(document.createTextNode(c.title || c.text || (c.body && c.body.text) || ''));
      return;
    }
    if (type === 'reaction') { bubble.appendChild(document.createTextNode('Reagiu ' + (c.emoji || ''))); return; }
    if (type === 'contacts') { bubble.appendChild(el('span', 'tag', '👤 Contato compartilhado')); return; }
    bubble.appendChild(el('span', 'tag', 'Mensagem do tipo “' + type + '” (veja no WhatsApp)'));
  }

  function renderMessages() {
    var list = Object.keys(state.messages).map(function (k) { return state.messages[k]; });
    list.sort(function (a, b) { return (a.created_at || '') < (b.created_at || '') ? -1 : 1; });
    els.messages.innerHTML = '';
    if (!list.length) {
      els.messages.appendChild(el('div', 'day-sep', 'Nenhuma mensagem nesta conversa ainda.'));
      return;
    }
    var lastDay = null;
    list.forEach(function (m) {
      var d = parseDate(m.created_at) || new Date();
      var day = d.toDateString();
      if (day !== lastDay) { els.messages.appendChild(el('div', 'day-sep', dayLabel(d))); lastDay = day; }
      var bubble = el('div', 'bubble' + (m.direction === 'outbound' ? ' out' : ''));
      renderContent(m, bubble);
      if (m.status === 'failed') {
        var reason = (m.error && (m.error.message || m.error.title)) || 'Falha no envio';
        bubble.appendChild(el('span', 'err', reason));
      }
      var meta = el('div', 'bubble-meta', d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' }));
      var icon = statusIcon(m);
      if (icon) meta.appendChild(icon);
      bubble.appendChild(meta);
      els.messages.appendChild(bubble);
    });
  }

  // ----------------------------------------------------------------- envio
  function sendText() {
    var body = els.text.value.trim();
    if (!body || state.sending || !state.activeId) return;
    state.sending = true;
    els.text.disabled = true;
    AZ.json(API + '/conversations/' + state.activeId + '/messages', { method: 'POST', body: { type: 'text', body: body } })
      .then(function (data) {
        els.text.value = '';
        autoGrow();
        state.messages[data.message.id] = data.message;
        renderMessages();
        els.messages.scrollTop = els.messages.scrollHeight;
        loadConversations();
      })
      .catch(function (err) { AZ.toast(err.message, 'error'); if (err.data && err.data.code === 'window_closed') { state.windowOpen = false; } })
      .then(function () { state.sending = false; updateComposer(); if (state.windowOpen) els.text.focus(); });
  }
  function autoGrow() {
    els.text.style.height = 'auto';
    els.text.style.height = Math.min(els.text.scrollHeight, 160) + 'px';
  }
  if (els.composer) {
    els.composer.addEventListener('submit', function (e) { e.preventDefault(); sendText(); });
    els.text.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendText(); }
    });
    els.text.addEventListener('input', autoGrow);
  }

  // -------------------------------------------------------- ações do topo
  if (els.assign) els.assign.addEventListener('click', function () {
    var c = state.active; if (!c) return;
    var mine = c.assigned_to === me;
    AZ.json(API + '/conversations/' + c.id, { method: 'POST', body: { assign: mine ? null : 'me' } })
      .then(function () { c.assigned_to = mine ? null : me; renderHeader(); AZ.toast(mine ? 'Conversa liberada.' : 'Conversa atribuída a você.', 'success'); loadConversations(); })
      .catch(function (err) { AZ.toast(err.message, 'error'); });
  });
  if (els.resolve) els.resolve.addEventListener('click', function () {
    var c = state.active; if (!c) return;
    var next = c.status === 'resolved' ? 'open' : 'resolved';
    AZ.json(API + '/conversations/' + c.id, { method: 'POST', body: { status: next } })
      .then(function () {
        c.status = next;
        renderHeader();
        AZ.toast(next === 'resolved' ? 'Conversa resolvida.' : 'Conversa reaberta.', 'success');
        loadConversations();
      })
      .catch(function (err) { AZ.toast(err.message, 'error'); });
  });
  if (els.back) els.back.addEventListener('click', function () {
    root.classList.remove('has-active');
    if (history.replaceState) history.replaceState(null, '', window.location.pathname);
    state.activeId = null;
    els.chat.hidden = true;
    els.empty.hidden = false;
    renderList();
  });

  // ---------------------------------------------------------------- modelos
  function templatePicker(listEl, paramsEl, sendBtn, connectionId) {
    var picked = null;
    listEl.innerHTML = '';
    listEl.appendChild(el('div', 'muted', 'Carregando modelos…'));
    paramsEl.innerHTML = '';
    sendBtn.disabled = true;
    AZ.json(API + '/templates?connection_id=' + encodeURIComponent(connectionId)).then(function (data) {
      listEl.innerHTML = '';
      var items = data.templates || [];
      if (!items.length) {
        listEl.appendChild(el('div', 'muted', 'Nenhum modelo aprovado. Crie um na página do número e aguarde a aprovação da Meta.'));
        return;
      }
      items.forEach(function (t) {
        var b = el('button', 'tpl-item');
        b.type = 'button';
        b.appendChild(el('b', null, t.name + ' · ' + t.language));
        b.appendChild(el('span', null, t.body));
        b.addEventListener('click', function () {
          listEl.querySelectorAll('.tpl-item').forEach(function (x) { x.classList.remove('active'); });
          b.classList.add('active');
          picked = t;
          paramsEl.innerHTML = '';
          for (var i = 1; i <= t.variables; i++) {
            var f = el('div', 'field');
            var id = 'tplvar_' + listEl.id + '_' + i;
            var lab = el('label', null, 'Variável {{' + i + '}}');
            lab.setAttribute('for', id);
            var inp = el('input');
            inp.id = id; inp.required = true; inp.setAttribute('data-var', String(i));
            f.appendChild(lab); f.appendChild(inp);
            paramsEl.appendChild(f);
          }
          sendBtn.disabled = false;
        });
        listEl.appendChild(b);
      });
    }).catch(function (err) {
      listEl.innerHTML = '';
      listEl.appendChild(el('div', 'muted', 'Não foi possível carregar os modelos: ' + err.message));
    });
    return function collect() {
      if (!picked) return null;
      var params = [];
      var ok = true;
      paramsEl.querySelectorAll('[data-var]').forEach(function (inp) {
        if (!inp.value.trim()) ok = false;
        params.push(inp.value.trim());
      });
      if (!ok) return false;
      return { name: picked.name, language: picked.language, params: params };
    };
  }

  var tplDialog = document.getElementById('templateDialog');
  var collectTpl = null;
  function openTemplateDialog() {
    if (!state.active || !tplDialog) return;
    document.getElementById('tplError').hidden = true;
    collectTpl = templatePicker(document.getElementById('tplList'), document.getElementById('tplParams'),
      document.getElementById('tplSend'), state.active.channel.id);
    tplDialog.showModal();
  }
  ['openTemplate', 'composerTemplate'].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener('click', openTemplateDialog);
  });
  var tplSend = document.getElementById('tplSend');
  if (tplSend) tplSend.addEventListener('click', function () {
    var tpl = collectTpl && collectTpl();
    var errBox = document.getElementById('tplError');
    if (!tpl) { errBox.hidden = false; document.getElementById('tplErrorText').textContent = 'Escolha um modelo e preencha todas as variáveis.'; return; }
    tplSend.disabled = true; tplSend.classList.add('is-loading');
    AZ.json(API + '/conversations/' + state.activeId + '/messages', { method: 'POST', body: { type: 'template', template: tpl } })
      .then(function (data) {
        tplDialog.close();
        state.messages[data.message.id] = data.message;
        renderMessages();
        els.messages.scrollTop = els.messages.scrollHeight;
        AZ.toast('Modelo enviado.', 'success');
        loadConversations();
      })
      .catch(function (err) { errBox.hidden = false; document.getElementById('tplErrorText').textContent = err.message; })
      .then(function () { tplSend.disabled = false; tplSend.classList.remove('is-loading'); });
  });

  // ---------------------------------------------------------- nova conversa
  var newDialog = document.getElementById('newDialog');
  var newChannel = document.getElementById('newChannel');
  var collectNew = null;
  function reloadNewTemplates() {
    if (!newChannel || !newChannel.value) return;
    collectNew = templatePicker(document.getElementById('newTplList'), document.getElementById('newTplParams'),
      document.getElementById('newSend'), newChannel.value);
  }
  if (newDialog) {
    document.querySelectorAll('[data-open="newDialog"]').forEach(function (b) {
      b.addEventListener('click', function () { document.getElementById('newError').hidden = true; reloadNewTemplates(); });
    });
    newChannel.addEventListener('change', reloadNewTemplates);
    var newSend = document.getElementById('newSend');
    newSend.addEventListener('click', function () {
      var tpl = collectNew && collectNew();
      var to = document.getElementById('newTo').value.trim();
      var errBox = document.getElementById('newError');
      if (!tpl || !to) { errBox.hidden = false; document.getElementById('newErrorText').textContent = 'Informe o contato, escolha um modelo e preencha as variáveis.'; return; }
      newSend.disabled = true; newSend.classList.add('is-loading');
      AZ.json(API + '/conversations/new', { method: 'POST', body: { connection_id: newChannel.value, to: to, template: tpl } })
        .then(function (data) {
          newDialog.close();
          AZ.toast('Conversa iniciada.', 'success');
          state.filter = 'open';
          setFilterButtons();
          return loadConversations().then(function () { if (data.conversation_id) openConversation(data.conversation_id); });
        })
        .catch(function (err) { errBox.hidden = false; document.getElementById('newErrorText').textContent = err.message; })
        .then(function () { newSend.disabled = false; newSend.classList.remove('is-loading'); });
    });
  }

  // ------------------------------------------------------ filtros e busca
  function setFilterButtons() {
    document.querySelectorAll('[data-filter]').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-filter') === state.filter);
    });
  }
  document.querySelectorAll('[data-filter]').forEach(function (b) {
    b.addEventListener('click', function () { state.filter = b.getAttribute('data-filter'); setFilterButtons(); loadConversations(); });
  });
  var searchTimer = null;
  if (els.search) els.search.addEventListener('input', function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.query = els.search.value.trim(); loadConversations(); }, 300);
  });

  // --------------------------------------------------------------- polling
  function visible() { return document.visibilityState === 'visible'; }
  setInterval(function () { if (visible()) loadConversations(); }, 6000);
  setInterval(function () { if (visible() && state.activeId) loadMessages(false); }, 3000);
  document.addEventListener('visibilitychange', function () { if (visible()) { loadConversations(); loadMessages(false); } });
  loadConversations().then(function () {
    var m = /^#c=([0-9a-fA-F-]{32,36})$/.exec(window.location.hash);
    if (m) openConversation(m[1]);
  });
})();
