/* Minimal enhancement: submit the controls when the fee mode changes. */
(function () {
  "use strict";
  var form = document.querySelector("form.controls");
  if (!form) return;
  var fee = form.querySelector("select[name=fee]");
  if (fee) fee.addEventListener("change", function () { form.submit(); });
})();
