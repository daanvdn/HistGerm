"""Showcase the primary HistGerm catalog operations."""

from pprint import pprint

from histgerm import load_catalog


def show_queries() -> None:
    """Find corpora, tools, and dictionaries."""

    catalog = load_catalog()

    corpora = catalog.find_corpora(stage="mhg")
    tools = catalog.find_tools(task="pos_tagger", stage="mhg")
    dictionaries = catalog.find_dictionaries(
        stage="mhg",
        machine_readable=True,
    )

    pprint(corpora)
    pprint(tools)
    pprint(dictionaries)


def show_analysis() -> None:
    """Inspect legal, overlap, and coverage metadata for matching texts."""

    catalog = load_catalog()
    texts = catalog.find_texts(stage="mhg")

    pprint(catalog.legal_warnings(texts))
    pprint(catalog.overlap_warnings(texts))
    pprint(catalog.coverage_summary(texts, by=["stage", "dialect"]))


def main() -> None:
    """Run all examples."""

    show_queries()
    show_analysis()


if __name__ == "__main__":
    main()
