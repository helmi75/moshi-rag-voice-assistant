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

// Thème (ASSISTANTE-114) : la page bascule au clic, sans attendre le serveur ; la requête
// htmx du formulaire ne fait que retenir le choix. Sans htmx, le formulaire part et la
// page revient déjà dans le bon thème.
document.addEventListener("click", (event) => {
  const bouton = event.target instanceof Element
    ? event.target.closest(".theme-choix button[value]") : null;
  if (!bouton) return;
  const racine = document.documentElement;
  if (bouton.value === "auto") racine.removeAttribute("data-theme");
  else racine.setAttribute("data-theme", bouton.value);
  document.querySelector('meta[name="color-scheme"]')
    ?.setAttribute("content", bouton.value === "auto" ? "light dark" : bouton.value);
  for (const autre of bouton.form.querySelectorAll("button[value]")) {
    autre.setAttribute("aria-pressed", String(autre === bouton));
  }
});

// Graphiques (ASSISTANTE-115) : au survol, au toucher ou au clavier, une bulle donne la
// date et la valeur de la barre — un jour sans activité compris. Le texte vient des
// attributs data-* posés par charts.py et entre par textContent : jamais du balisage.
const bulle = document.createElement("div");
bulle.className = "viz-bulle";
bulle.setAttribute("role", "tooltip");
bulle.hidden = true;
const bulleValeur = document.createElement("strong");
const bulleQuand = document.createElement("span");
bulle.append(bulleValeur, bulleQuand);
document.body.append(bulle);

function barreVisee(event) {
  return event.target instanceof Element ? event.target.closest(".viz-barre") : null;
}

function montrerBulle(barre) {
  bulleValeur.textContent = barre.dataset.valeur || "";
  bulleQuand.textContent = barre.dataset.quand || "";
  bulle.hidden = false;
  // Au-dessus de la barre ; au-dessus de la ligne de base pour un jour à zéro ; en
  // dessous si le haut de l'écran manque.
  const colonne = barre.getBoundingClientRect();
  const marque = barre.querySelector(".viz-marque");
  const sommet = marque ? marque.getBoundingClientRect().top : colonne.bottom;
  const taille = bulle.getBoundingClientRect();
  const gauche = Math.min(Math.max(8, colonne.left + colonne.width / 2 - taille.width / 2),
                          window.innerWidth - taille.width - 8);
  let haut = sommet - taille.height - 10;
  if (haut < 8) haut = colonne.bottom + 10;
  bulle.style.left = `${gauche}px`;
  bulle.style.top = `${haut}px`;
}

function cacherBulle() {
  bulle.hidden = true;
}

document.addEventListener("pointerover", (event) => {
  const barre = barreVisee(event);
  if (barre) montrerBulle(barre);
  else if (!bulle.hidden) cacherBulle();
});
document.addEventListener("pointerout", (event) => {
  if (!event.relatedTarget) cacherBulle();  // le pointeur quitte la fenêtre
});
document.addEventListener("focusin", (event) => {
  const barre = barreVisee(event);
  if (barre) montrerBulle(barre);
});
document.addEventListener("focusout", (event) => {
  if (barreVisee(event)) cacherBulle();
});
window.addEventListener("scroll", cacherBulle, { passive: true });

// Au clavier, un graphique est UN arrêt de tabulation (le dernier jour) : les flèches
// parcourent les jours, au lieu de trente arrêts par graphique.
document.addEventListener("keydown", (event) => {
  const barre = barreVisee(event);
  if (!barre || !barre.ownerSVGElement) return;
  const barres = [...barre.ownerSVGElement.querySelectorAll(".viz-barre")];
  const i = barres.indexOf(barre);
  const cible = barres[{ ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0,
                         End: barres.length - 1 }[event.key]];
  if (!cible) return;
  event.preventDefault();
  barre.setAttribute("tabindex", "-1");
  cible.setAttribute("tabindex", "0");
  cible.focus();
});
