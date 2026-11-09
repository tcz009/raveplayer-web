/* Login page: password reveal plus duplicate-submit protection.
 *
 * A double-click or a second Enter used to send POST /login twice.  The first
 * request authenticated and rotated the session CSRF token; the duplicate
 * arrived with the old form token and painted a "session expired" page over a
 * successful sign-in.  The form is now single-shot (the server also treats an
 * already-authenticated duplicate as a redirect, so the guard is belt and
 * braces for clients with JS disabled).
 *
 * No inline script, no innerHTML.
 */
(function () {
  var toggle = document.getElementById("password-toggle");
  var input = document.getElementById("password");
  if (toggle && input) {
    toggle.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      toggle.setAttribute("aria-pressed", String(!showing));
      toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
      toggle.textContent = showing ? "Show" : "Hide";
      input.focus({ preventScroll: true });
    });
  }

  var form = document.getElementById("login-form");
  if (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.submitting === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.submitting = "1";
      var submit = document.getElementById("login-submit");
      if (submit) {
        submit.disabled = true;
        submit.textContent = "Entering…";
      }
    });
  }

  // Safari restores login pages from the back/forward cache with the CSRF
  // token that was rendered before a later login rotated it.  Reloading once
  // gives the form the token that matches the current session cookie.
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      window.location.reload();
    }
  });
})();
