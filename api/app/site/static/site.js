(function () {
  "use strict";
  var calme = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- le rappel : le numéro part à /rappel, et Marie appelle ---- */
  function chiffres(brut) {
    var s = String(brut || "").replace(/[\s.\-()]/g, "");
    if (s.indexOf("+33") === 0) s = "0" + s.slice(3);
    if (s.indexOf("0033") === 0) s = "0" + s.slice(4);
    return s;
  }
  function lisible(s) { return s.replace(/(\d{2})(?=\d)/g, "$1 ").trim(); }

  document.querySelectorAll("form.rappel").forEach(function (form) {
    var champ = form.querySelector('input[type="tel"]');
    var piege = form.querySelector(".rappel__piege");
    var bouton = form.querySelector('button[type="submit"]');
    var erreur = form.querySelector(".rappel__erreur");
    var saisie = form.querySelector('[data-etat="saisie"]');
    var fait = form.querySelector('[data-etat="fait"]');
    var libelle = bouton.innerHTML;

    function refuser(message) {
      erreur.textContent = message;
      erreur.hidden = false;
      champ.focus();
    }
    function attendre(oui) {
      bouton.disabled = oui;
      if (oui) bouton.textContent = "Un instant…"; else bouton.innerHTML = libelle;
    }

    champ.addEventListener("input", function () { erreur.hidden = true; });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (bouton.disabled) return;
      var numero = chiffres(champ.value);
      if (!/^0[1-9]\d{8}$/.test(numero)) {
        refuser("Ce numéro semble incomplet. Exemple : 06 12 34 56 78.");
        return;
      }
      erreur.hidden = true;
      attendre(true);
      fetch("/rappel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ numero: numero, site: piege ? piege.value : "" })
      }).then(function (reponse) {
        return reponse.json().then(function (corps) { return { ok: reponse.ok, corps: corps }; });
      }).then(function (resultat) {
        attendre(false);
        var message = (resultat.corps && resultat.corps.message) || "";
        if (!resultat.ok) {
          refuser(message || "La demande n'a pas pu partir. Réessayez dans un instant.");
          return;
        }
        form.querySelector("[data-message]").textContent = message;
        form.querySelector("[data-numero]").textContent = "(" + lisible(numero) + ")";
        saisie.hidden = true;
        fait.hidden = false;
      }).catch(function () {
        attendre(false);
        refuser("La demande n'a pas pu partir. Vérifiez votre connexion et réessayez.");
      });
    });
    form.querySelector("[data-retour]").addEventListener("click", function () {
      fait.hidden = true;
      saisie.hidden = false;
      champ.focus();
    });
  });

  /* tous les boutons « être rappelé » ramènent au champ du haut, prêt à écrire */
  document.querySelectorAll("[data-vers-rappel]").forEach(function (lien) {
    lien.addEventListener("click", function (event) {
      event.preventDefault();
      var champ = document.getElementById("tel-haut");
      champ.closest(".rappel-zone").scrollIntoView({ behavior: calme ? "auto" : "smooth", block: "center" });
      window.setTimeout(function () { champ.focus({ preventScroll: true }); }, calme ? 0 : 450);
    });
  });

  /* ---- l'appel se rejoue, tour par tour ---- */
  var combine = document.getElementById("combine");
  var bouton = document.getElementById("rejouer");
  if (combine && bouton) {
    if (calme) bouton.hidden = true;
    bouton.addEventListener("click", function () {
      var pieces = combine.querySelectorAll(".tour, .resa");
      pieces.forEach(function (piece, i) { piece.style.setProperty("--d", (i * 520) + "ms"); });
      combine.classList.remove("rejoue");
      void combine.offsetWidth;
      combine.classList.add("rejoue");
    });
  }

  /* ---- le calcul ---- */
  var euros = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
  var nombre = new Intl.NumberFormat("fr-FR");
  var c = {
    manques: document.getElementById("manques"), part: document.getElementById("part"),
    addition: document.getElementById("addition"), couverts: document.getElementById("couverts")
  };
  /* le prix de la formule est écrit par le serveur, d'après la grille en vigueur */
  var formule = +document.querySelector(".ticket").getAttribute("data-formule") || 0;
  function calculer() {
    var manques = +c.manques.value, part = +c.part.value / 100;
    var addition = +c.addition.value, parTable = +c.couverts.value / 10;
    var resas = manques * 30 * part, couverts = resas * parTable, ca = couverts * addition;
    document.getElementById("manques-v").textContent = manques;
    document.getElementById("part-v").textContent = c.part.value + " %";
    document.getElementById("addition-v").textContent = c.addition.value + " €";
    document.getElementById("couverts-v").textContent = parTable.toLocaleString("fr-FR");
    document.getElementById("t-resas").textContent = nombre.format(Math.round(resas)) + " par mois";
    document.getElementById("t-couverts").textContent = nombre.format(Math.round(couverts));
    document.getElementById("t-ca").textContent = euros.format(Math.round(ca));
    document.getElementById("t-net").textContent = euros.format(Math.round(ca - formule));
  }
  Object.keys(c).forEach(function (k) { c[k].addEventListener("input", calculer); });
  calculer();
})();
