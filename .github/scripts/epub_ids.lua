-- Used by build_epub.py.
-- Header IDs must not clash with the section pandoc's EPUB writer creates
-- from the title (it does this when the essay has no level-1 headings).
local seen = {}
local reserved = nil
function Meta(m)
  if m.title then
    reserved = pandoc.utils.stringify(m.title):lower()
      :gsub("[^%w%s%-_%.]", ""):gsub("%s+", "-"):gsub("^[^%a]+", "")
  end
end
function Header(h)
  local id = h.identifier
  if id ~= "" and (id == reserved or seen[id]) then
    local n = 1
    while seen[id .. "-" .. n] or (id .. "-" .. n) == reserved do n = n + 1 end
    h.identifier = id .. "-" .. n
  end
  seen[h.identifier] = true
  return h
end
return {{Meta = Meta}, {Header = Header}}
