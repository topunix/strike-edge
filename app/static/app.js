/* Minimal enhancement: submit the controls when the fee mode changes (htmx handles it when loaded). */
(function () {
  "use strict";
  var form = document.querySelector("form.controls");
  if (!form) return;
  var fee = form.querySelector("select[name=fee]");
  if (fee) fee.addEventListener("change", function () { if (!window.htmx) form.submit(); });
})();
