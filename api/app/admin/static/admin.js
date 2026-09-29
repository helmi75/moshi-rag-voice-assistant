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

// Une saisie refusée (HTTP 422) ou un conflit (HTTP 409 : la base a changé entre-temps,
// « Ce que l'IA sait ») revient avec le fragment et son message d'erreur : htmx 1.9
// n'affiche pas les réponses 4xx par défaut, et la ligne resterait figée sans dire
// pourquoi l'enregistrement n'a pas eu lieu.
document.addEventListener("htmx:beforeSwap", (event) => {
  if (event.detail.xhr.status === 422 || event.detail.xhr.status === 409) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});

// Réservations : sur un téléphone, la grille du mois est illisible — la vue du jour
// s'ouvre d'elle-même, tant qu'aucune vue n'a été choisie (SCRUM-112).
if (document.querySelector("[data-vue-implicite]")
    && window.matchMedia("(max-width: 640px)").matches) {
  const adresse = new URL(window.location.href);
  adresse.searchParams.set("vue", "jour");
  window.location.replace(adresse.toString());
}

// Page « Voix & accueil » : le lecteur fait entendre la voix SÉLECTIONNÉE dans la liste,
// avant même de l'enregistrer. L'adresse de l'extrait est portée par l'option.
document.addEventListener("change", (event) => {
  const liste = event.target;
  if (!(liste instanceof HTMLSelectElement) || !liste.dataset.extraitCible) return;
  const lecteur = document.getElementById(liste.dataset.extraitCible);
  const adresse = liste.selectedOptions[0]?.dataset.extrait;
  if (lecteur instanceof HTMLAudioElement && adresse) {
    lecteur.pause();
    lecteur.src = adresse;
  }
});
