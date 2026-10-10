/* Reset password — extrai access_token do fragmento da URL */
(function () {
  var hash = window.location.hash || '';
  var tokenMatch = hash.match(/access_token=([^&]+)/);
  var token = tokenMatch ? decodeURIComponent(tokenMatch[1]) : '';
  var input = document.getElementById('access_token');
  if (input) {
    input.value = token;
  }
  if (!token) {
    var form = document.getElementById('resetForm');
    if (form) {
      var notice = document.createElement('div');
      notice.className = 'alert alert-error';
      notice.textContent = 'Token de recuperação ausente ou expirado. Solicite um novo link.';
      form.parentNode.insertBefore(notice, form);
    }
  }
})();
