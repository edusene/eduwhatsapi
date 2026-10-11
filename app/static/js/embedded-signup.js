/* Embedded Signup (WhatsApp) com o SDK oficial da Meta.
   Referência: developers.facebook.com → WhatsApp → Embedded Signup → Implementation.

   1. Carrega https://connect.facebook.net/en_US/sdk.js e chama FB.init.
   2. FB.login({config_id, response_type: 'code', override_default_response_type: true,
      extras: {setup: {}, [featureType]}}) abre o fluxo da Meta.
   3. A Meta envia window.postMessage {type: 'WA_EMBEDDED_SIGNUP', event: 'FINISH'...,
      data: {phone_number_id, waba_id, business_id}} (só aceitamos origem *.facebook.com).
   4. O callback do FB.login traz authResponse.code (válido por ~30s), que enviamos
      ao servidor junto com os IDs. O servidor troca o código pelo token e valida
      tudo na Graph API — nada é considerado conectado só pelo navegador. */
(function () {
  'use strict';
  var cfg = document.getElementById('esConfig');
  if (!cfg || cfg.getAttribute('data-enabled') !== 'true') return;

  var dialog = document.getElementById('esDialog');
  var startBtn = document.getElementById('esStart');
  var pinField = document.getElementById('esPinField');
  var errorBox = document.getElementById('esError');
  var statusBox = document.getElementById('esStatus');
  if (!dialog || !startBtn) return; // usuário sem permissão de conectar
  var mode = 'standard';
  var sessionInfo = null;
  var code = null;
  var sdkReady = false;

  function showError(text) {
    statusBox.hidden = true;
    errorBox.hidden = false;
    document.getElementById('esErrorText').textContent = text;
    startBtn.disabled = false;
    startBtn.classList.remove('is-loading');
  }
  function showStatus(text) {
    errorBox.hidden = true;
    statusBox.hidden = false;
    document.getElementById('esStatusText').textContent = text;
  }

  window.fbAsyncInit = function () {
    FB.init({
      appId: cfg.getAttribute('data-app-id'),
      autoLogAppEvents: true,
      xfbml: false,
      version: cfg.getAttribute('data-version')
    });
    sdkReady = true;
  };
  var script = document.createElement('script');
  script.src = 'https://connect.facebook.net/en_US/sdk.js';
  script.async = true;
  script.defer = true;
  script.crossOrigin = 'anonymous';
  script.onerror = function () { sdkReady = false; };
  document.head.appendChild(script);

  window.addEventListener('message', function (event) {
    var host;
    try { host = new URL(event.origin).hostname; } catch (e) { return; }
    if (host !== 'facebook.com' && !host.endsWith('.facebook.com')) return;
    var data;
    try { data = typeof event.data === 'string' ? JSON.parse(event.data) : event.data; } catch (e) { return; }
    if (!data || data.type !== 'WA_EMBEDDED_SIGNUP') return;
    if (String(data.event || '').indexOf('FINISH') === 0) {
      sessionInfo = data.data || {};
      maybeComplete();
    } else if (data.event === 'CANCEL') {
      var step = data.data && data.data.current_step ? ' (etapa: ' + data.data.current_step + ')' : '';
      var reported = data.data && data.data.error_message ? ': ' + data.data.error_message : step;
      showError('O fluxo da Meta foi interrompido' + reported + '.');
    }
  });

  function maybeComplete() {
    if (!code || !sessionInfo) return;
    if (!sessionInfo.phone_number_id) {
      showError('Nenhum número foi selecionado no fluxo da Meta. Refaça o processo escolhendo um número.');
      return;
    }
    showStatus('Finalizando a conexão com a Meta…');
    var payload = {
      code: code,
      waba_id: sessionInfo.waba_id,
      phone_number_id: sessionInfo.phone_number_id,
      business_id: sessionInfo.business_id,
      coexistence: mode === 'coexistence',
      pin: mode === 'coexistence' ? '' : (document.getElementById('es_pin').value || '').trim()
    };
    code = null;
    AZ.json(cfg.getAttribute('data-endpoint'), { method: 'POST', body: payload })
      .then(function (resp) { window.location.assign(resp.redirect); })
      .catch(function (err) { showError(err.message); });
  }

  function launch() {
    errorBox.hidden = true;
    if (!sdkReady || typeof FB === 'undefined') {
      showError('Não foi possível carregar o SDK da Meta. Verifique bloqueadores de anúncio/rastreamento e tente de novo.');
      return;
    }
    var pin = (document.getElementById('es_pin').value || '').trim();
    if (mode !== 'coexistence' && pin && !/^\d{6}$/.test(pin)) {
      showError('O PIN deve ter exatamente 6 dígitos (ou deixe em branco para registrar depois).');
      return;
    }
    sessionInfo = null;
    code = null;
    startBtn.disabled = true;
    startBtn.classList.add('is-loading');
    showStatus('Conclua as etapas na janela da Meta…');
    var extras = { setup: {} };
    if (mode === 'coexistence') extras.featureType = 'whatsapp_business_app_onboarding';
    FB.login(function (response) {
      if (response && response.authResponse && response.authResponse.code) {
        code = response.authResponse.code;
        maybeComplete();
        // Se o evento FINISH não chegar (ex.: fluxo encerrado sem número), avisa.
        setTimeout(function () {
          if (code && !sessionInfo) showError('A Meta não informou o número escolhido. Tente novamente.');
        }, 8000);
      } else {
        showError('Autorização cancelada ou não concedida.');
      }
    }, {
      config_id: cfg.getAttribute('data-config-id'),
      response_type: 'code',
      override_default_response_type: true,
      extras: extras
    });
  }

  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-es-start]');
    if (!trigger) return;
    mode = trigger.getAttribute('data-es-start');
    var parent = trigger.closest('dialog[open]');
    if (parent) parent.close();
    pinField.hidden = mode === 'coexistence';
    document.getElementById('esTitle').textContent =
      mode === 'coexistence' ? 'Conectar com coexistência' : 'Cadastro oficial da Meta';
    errorBox.hidden = true;
    statusBox.hidden = true;
    startBtn.disabled = false;
    startBtn.classList.remove('is-loading');
    dialog.showModal();
  });
  startBtn.addEventListener('click', launch);
})();
