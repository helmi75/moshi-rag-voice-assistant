def retrieve_context(query: str, top_k: int = 3):
    # Demo static contexts. Replace with FAISS/Chroma + embeddings in production.
    docs = [
        "Le Fouquet's Paris, 99 avenue des Champs-Élysées, hôtel Barrière. Téléphone 01 40 69 60 50.",
        "Services : petit-déjeuner dès 7h30, brunch le week-end, terrasse chauffée, service continu.",
        "Menus : formule déjeuner à partir de 78 €, options sans gluten et végétariennes disponibles."
    ]
    return docs[:top_k]
