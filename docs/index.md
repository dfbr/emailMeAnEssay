---
layout: default
title: Essay Library
---

# Essay Library

Browse essays and choose to read online or download EPUB.

{% assign essays = site.data.essays | default: empty %}
{% if essays.size == 0 %}
No essays published yet.
{% else %}
<ul class="essay-list">
{% for essay in essays %}
  <li class="essay-card">
    <h2>{{ essay.title }}</h2>
    <p class="meta">{{ essay.generated_at }} • {{ essay.word_count }} words</p>
    <p>{{ essay.excerpt }}</p>
    <p class="actions">
      <a href="{{ essay.read_path | relative_url }}">Read online</a>
      <a href="{{ essay.epub_path | relative_url }}">Download EPUB</a>
    </p>
  </li>
{% endfor %}
</ul>
{% endif %}
