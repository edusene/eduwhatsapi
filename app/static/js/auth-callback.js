/* Links de e-mail do Supabase Auth: os tokens chegam no fragmento (#) da URL,
   que o servidor não enxerga. Este script os lê e:
   - recuperação de senha: preenche o formulário de nova senha;
   - confirmação de cadastro: envia o access_token ao servidor, que o valida
     no Supabase (auth.get_user) antes de criar a sessão.
   O fragmento é removido da barra de endereço logo em seguida. */
(function () {
  'use strict';
  var params = new URLSearchParams((window.location.hash || '').replace(/^#/, ''));
  var token = params.get('access_token') || '';
  var type = params.get('type') || '';
  var errorDesc = params.get('error_description');
  if (window.location.hash) history.replaceState(null, '', window.location.pathname);

  // Página de nova senha
  var resetInput = document.getElementById('access_token');
  if (resetInput) {
    if (token) {
      resetInput.value = token;
    } else {
      var missing = document.getElementById('resetMissing');
      var form = document.getElementById('resetForm');
      if (missing) missing.hidden = false;
      if (form) form.hidden = true;
    }
    return;
  }

  // Página de callback (confirmação de e-mail)
  var state = document.getElementById('callbackState');
  if (!state) return;
  var msg = document.getElementById('callbackMsg');
  function fail(text) {
    msg.textContent = text;
    var link = document.createElement('a');
    link.className = 'btn btn-primary mt-2';
    link.href = state.getAttribute('data-login-url');
    link.textContent = 'Ir para o login';
    state.appendChild(link);
  }
  if (errorDesc) { fail('O link não é mais válido: ' + errorDesc); return; }
  if (!token) { fail('Link inválido ou já utilizado.'); return; }
  if (type === 'recovery') {
    window.location.replace(state.getAttribute('data-reset-url') + '#access_token=' + encodeURIComponent(token) + '&type=recovery');
    return;
  }
  AZ.json(state.getAttribute('data-session-url'), { method: 'POST', body: { access_token: token } })
    .then(function (data) { window.location.replace(data.redirect || '/dashboard/'); })
    .catch(function (err) { fail(err.message || 'Não foi possível confirmar seu acesso.'); });
})();
