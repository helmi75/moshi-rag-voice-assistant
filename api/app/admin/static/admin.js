// Comportements sans script en ligne.
//
// Les filtres se soumettaient par `onchange="this.form.submit()"` en attribut. La CSP
// range ces attributs avec le script en ligne : les autoriser imposerait
// `script-src 'unsafe-inline'`, ce qui rendrait la CSP à peu près inutile — or c'est
// précisément elle qui protège l'admin si un nom d'établissement ou une note de
// réservation contenait un jour du balisage.
document.addEventListener("change", (event) => {
  const champ = event.target;
  if (champ instanceof Element && champ.hasAttribute("data-autosubmit")) {
    champ.form?.submit();
  }
});

// Une saisie refusée (HTTP 422) revient avec le fragment et son message d'erreur : htmx
// 1.9 n'affiche pas les réponses 4xx par défaut, et la ligne resterait figée sans dire
// pourquoi l'enregistrement n'a pas eu lieu.
document.addEventListener("htmx:beforeSwap", (event) => {
  if (event.detail.xhr.status === 422) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});
