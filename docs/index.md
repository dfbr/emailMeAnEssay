---
layout: default
title: Essay Library
---

# Essay Library

Every now and then I get curious about something and ask an LLM to generate an essay for me on a topic. Here are the results in case anyone else is interested in them. The usual caveats for LLM generated content apply _mistakes happen_. 

{% assign essays = site.data.essays | default: empty %}
{% if essays.size == 0 %}
No essays published yet.
{% else %}
<ul class="essay-list">
{% for essay in essays %}
  <li class="essay-card">
    <h2><a href="{{ essay.read_path | relative_url }}">{{ essay.title }}</a></h2>
    {% if essay.preview_slug %}<p class="meta">{{ essay.preview_slug }}</p>{% endif %}
    <p class="meta">{{ essay.generated_at }} • {{ essay.word_count }} words</p>
    <p>{{ essay.preview_text | default: essay.excerpt }}</p>
    <p class="actions">
      <a href="{{ essay.read_path | relative_url }}">Read online</a>
      <a href="{{ essay.epub_path | relative_url }}">Download EPUB</a>
    </p>
  </li>
{% endfor %}
</ul>
{% endif %}
