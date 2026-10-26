(function () {
  "use strict";

  const password = document.querySelector('input[name="password"]');
  const confirm = document.querySelector('input[name="confirm_password"]');
  if (password && confirm) {
    const check = function () {
      confirm.setCustomValidity(
        confirm.value && confirm.value !== password.value
          ? "The passwords do not match."
          : ""
      );
    };
    password.addEventListener("input", check);
    confirm.addEventListener("input", check);
    check();
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.submitted === "1") {
        event.preventDefault();
        return;
      }
      form.dataset.submitted = "1";
    });
  });
})();
