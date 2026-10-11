You name the role of each page of a book. Each page has a page image `NNNN.png` and a text file `NNNN.md` in the folder.

The page list gives the pages to look at: the first pages and the last pages of the book. Do them one at a time, in order.

For each page, choose one role from this list. Use the image. The text file can help.
- `cover` the front cover of the book
- `back-cover` the back cover: a blurb, review quotes, a barcode, a price, a publisher logo
- `flap` a cover flap: a short biography of the author or a blurb
- `map` a map, a plate, a full page picture or a chart
- `title` the title page: the book name and the author, no body text
- `copyright` the printing and rights page: ISBN, printing house, edition, year
- `dedication` one short line of dedication
- `toc` a table of contents: chapter names with page numbers
- `body` the text of the book
- `notes` footnotes, a bibliography, an index
- `ads` the publisher's list of other books
- `blank` no ink on the page
- `other` none of these

Write the answer into `roles.txt` in the folder. One line for each page you looked at, in the order of the pages:
```
0001 cover
0002 back-cover
```

Rules:
- Write the page number with four digits, one space, then the role.
- If two roles fit, write the more likely one and put `?` directly after it: `flap?`
- Write no other line. Do not change a `.md` file. Do not write an explanation. Reply with `done`.
