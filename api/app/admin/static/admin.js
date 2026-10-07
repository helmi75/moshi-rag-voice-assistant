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
function appliquerTheme(formulaire, valeur) {
  const racine = document.documentElement;
  if (valeur === "auto") racine.removeAttribute("data-theme");
  else racine.setAttribute("data-theme", valeur);
  document.querySelector('meta[name="color-scheme"]')
    ?.setAttribute("content", valeur === "auto" ? "light dark" : valeur);
  for (const bouton of formulaire.querySelectorAll("button[value]")) {
    bouton.setAttribute("aria-pressed", String(bouton.value === valeur));
  }
}

document.addEventListener("click", (event) => {
  const bouton = event.target instanceof Element
    ? event.target.closest(".theme-choix button[value]") : null;
  if (!bouton) return;
  bouton.form.dataset.avant = document.documentElement.getAttribute("data-theme") || "auto";
  appliquerTheme(bouton.form, bouton.value);
});

// Le choix n'a pas pu être retenu (session expirée, jeton périmé, réseau coupé) : la page
// revient au thème d'avant, au lieu d'afficher un thème que la page suivante aurait perdu
// sans rien dire.
for (const echec of ["htmx:responseError", "htmx:sendError", "htmx:timeout"]) {
  document.addEventListener(echec, (event) => {
    const formulaire = event.target instanceof Element
      ? event.target.closest(".theme-choix") : null;
    if (formulaire?.dataset.avant) appliquerTheme(formulaire, formulaire.dataset.avant);
  });
}

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
  // Le <title> du SVG est le repli quand ce script ne tourne pas : ici, il ferait une
  // seconde infobulle, celle du navigateur, par-dessus la nôtre.
  barre.querySelector("title")?.remove();
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
  // La souris quitte la fenêtre. Au doigt, ce même événement suit CHAQUE levée du doigt :
  // la bulle se refermait aussitôt posée — elle reste jusqu'au toucher suivant ailleurs.
  if (event.pointerType === "mouse" && !event.relatedTarget) cacherBulle();
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

// Sur un téléphone, un graphique de trente jours défile dans sa carte (admin.css,
// `.viz-dense`) : on l'amène sur le jour le plus récent, celui qu'on vient y lire. Les
// graphiques arrivent par htmx, d'où le second branchement.
function allerAuDernierJour(racine) {
  racine.querySelectorAll(".viz-dense .chart-block").forEach((cadre) => {
    cadre.scrollLeft = cadre.scrollWidth;
  });
}
allerAuDernierJour(document);
document.addEventListener("htmx:afterSwap", (event) => {
  if (event.target instanceof Element) allerAuDernierJour(event.target);
});

// Menu du téléphone : le bouton aux trois barres déroule la navigation et la referme.
// L'état est porté par `data-ouvert` sur la barre (la feuille de style fait le reste) et
// par `aria-expanded` sur le bouton. Échap et un toucher hors de la barre referment.
function basculerMenu(ouvrir) {
  const barre = document.querySelector(".sidebar");
  const bouton = document.querySelector("[data-menu]");
  if (!barre || !bouton) return;
  barre.toggleAttribute("data-ouvert", ouvrir);
  bouton.setAttribute("aria-expanded", ouvrir ? "true" : "false");
}
document.addEventListener("click", (event) => {
  const barre = document.querySelector(".sidebar");
  if (!barre || !(event.target instanceof Element)) return;
  if (event.target.closest("[data-menu]")) {
    basculerMenu(!barre.hasAttribute("data-ouvert"));
  } else if (barre.hasAttribute("data-ouvert") && !event.target.closest(".sidebar")) {
    basculerMenu(false);
  }
});
document.addEventListener("keydown", (event) => {
  const barre = document.querySelector(".sidebar");
  if (event.key !== "Escape" || !barre || !barre.hasAttribute("data-ouvert")) return;
  basculerMenu(false);
  document.querySelector("[data-menu]")?.focus();
});

// Appels, sur téléphone : la page tient en une colonne, et la fiche tombait sous les
// vingt-cinq lignes de la liste. Sous 900 px (le seuil de la feuille de style), le nœud de
// la fiche est glissé juste sous la ligne ouverte, puis la ligne est amenée en haut de
// l'écran. Au-dessus, ou sans appel ouvert, il retrouve sa place dans `.split`. Le nœud est
// déplacé, pas copié : `#call-pane` reste unique. Sans script, l'ancre du lien suffit.
const ecranEtroit = window.matchMedia("(max-width: 900px)");

function placerLaFiche(faireDefiler) {
  const fiche = document.getElementById("call-pane");
  const vue = document.getElementById("calls-view");
  if (!fiche || !vue) return;
  const ligne = vue.querySelector('a.card-row[aria-current="true"]');
  if (ecranEtroit.matches && ligne) {
    if (ligne.nextElementSibling !== fiche) ligne.after(fiche);
    if (faireDefiler) {
      const calme = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      ligne.scrollIntoView({ block: "start", behavior: calme ? "auto" : "smooth" });
    }
  } else if (fiche.parentElement !== vue) {
    vue.append(fiche);
  }
}
placerLaFiche(true);
document.addEventListener("htmx:afterSettle", () => placerLaFiche(true));
ecranEtroit.addEventListener("change", () => placerLaFiche(false));
