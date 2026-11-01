/* Login page: password reveal only. No inline script, no innerHTML. */
(function () {
  var toggle = document.getElementById("password-toggle");
  var input = document.getElementById("password");
  if (!toggle || !input) return;
  toggle.addEventListener("click", function () {
    var showing = input.type === "text";
    input.type = showing ? "password" : "text";
    toggle.setAttribute("aria-pressed", String(!showing));
    toggle.setAttribute("aria-label", showing ? "Show password" : "Hide password");
    toggle.textContent = showing ? "Show" : "Hide";
    input.focus({ preventScroll: true });
  });
})();
