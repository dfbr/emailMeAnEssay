import re
import requests


def get_wikipedia_image_url(article_title: str) -> str | None:
    """Queries the Wikipedia API to find the main image of a given page."""
    api_url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageimages",
        "titles": article_title.replace(" ", "_"),
        "pithumbsize": 1000,  # Request high-res (1000px wide)
    }

    try:
        response = requests.get(api_url, params=params, timeout=10)
        data = response.json()
        pages = data.get("query", {}).get("pages", {})

        for page_id, page_data in pages.items():
            if "thumbnail" in page_data:
                return page_data["thumbnail"]["source"]
    except Exception:
        pass
    return None


def process_essay_images(input_file: str, output_file: str):
    with open(input_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Find pattern: <!-- IMAGE_REQUEST: Article_Title | query -->
    pattern = r"<!--\s*IMAGE_REQUEST:\s*([^|]+?)\s*\|\s*(.*?)\s*-->"

    def replace_image(match):
        article_title = match.group(1).strip()
        query = match.group(2).strip()

        print(f"Fetching image for Wikipedia article: '{article_title}'...")
        image_url = get_wikipedia_image_url(article_title)

        if image_url:
            return f"![Image related to {article_title}]({image_url})"
        else:
            # Fallback placeholder if the article didn't have an image or was misspelled
            return f"<!-- IMAGE_FALLBACK: {query} -->"

    final_content = re.sub(pattern, replace_image, content)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(final_content)

    print(f"\nSuccessfully finalized images into: {output_file}")


if __name__ == "__main__":
    # Point this to whatever raw markdown file your main script outputted
    process_essay_images("output/raw_essay.md", "output/final_ebook_essay.md")