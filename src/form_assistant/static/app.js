// Visa att Claude arbetar och förhindra dubbelklick (anropen kan ta en halv minut).
document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const button = event.submitter;
    if (button && button.name) {
      const hidden = document.createElement("input");
      hidden.type = "hidden"; hidden.name = button.name; hidden.value = button.value;
      form.appendChild(hidden);
    }
    form.classList.add("busy");
    form.querySelectorAll("button").forEach((b) => (b.disabled = true));
  });
});
