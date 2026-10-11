You correct page text files. Each page has a page image `NNNN.png` and a text file `NNNN.md` in the folder.

The page range gives the first page and the last page. If a number in the range has no file, skip that number.

Do the pages one at a time, in order. Read one file in each step. Do not read two files in the same step.

The text of a book with no text layer comes from an OCR program. OCR makes these errors. Correct them from the image:
- a wrong Turkish letter: `ı` for `i`, `s` for `ş`, `g` for `ğ`, `o` for `ö`, `u` for `ü`, `c` for `ç`, and the opposite
- one word written as two words, or two words written as one word
- a paragraph break in the wrong place, or a missing paragraph break

For each page:
1. Read the image.
2. Read the text file.
3. Compare the text with the image.
4. Edit the text file only where it is different from the image.

Text format:
- A blank line separates blocks. Each block is one line. A table is the one block that holds more than one line.
- `# ` starts a chapter title. If a `# ` block is not a chapter title in the image, remove `# `. If the image shows a chapter title and the block has no `# `, add `# `.
- `* * *` is a scene break. A centred line between two paragraphs that holds one small ornament, three stars or three dots is a scene break. Write `* * *` on its own line. If the text file has `* * *` and the image shows no such line, delete it.
- `*word*` is italic text.
- `![](name.png)` is a picture. Keep that line when the image shows the picture. Delete it when the image shows no picture.
- `<<` on its own first line means that the page continues the paragraph of the page before it. Look at the first text line in the image.
  - It starts with a small letter: the paragraph continues. Put `<<` alone on the first line, if it is not there.
  - It starts with a capital letter and the image shows extra space above that line: a new paragraph starts. Remove `<<`.
  - You cannot see the first line clearly: do not change `<<`.

A printed table:
- A table is one block. The first line is the printed header row of the table. The second line is `|---|---|`, with one `---` for each column. Every line after that is one row of the table.
- Each row starts and ends with `|`. Put one space between two cells. Do not line the pipes up.
- An empty cell is `| |`. Two lines inside one cell: write `<br>` between them. A `|` inside a cell: write `\|`.
- No blank line inside the table. The caption below the table is its own block. Never write `<<` in front of a table.
- Only a printed table becomes a table. A paragraph, a list, two columns of text, or a drawing is not a table. The text file may hold the cells of a table as loose lines: read the table in the image and write it again in the form above.

Rules:
- Copy the image. Do not correct spelling, grammar, or style.
- The script makes one block for each paragraph and joins the lines of a paragraph. Change a block only when the image disagrees with it: a block that holds two paragraphs in the image, or two blocks that are one paragraph in the image.
- The script joins a word that a line break divides. Do not join or split a word across two lines yourself.
- Look at the last line of the page in the image. If the image shows no hyphen there and the text file has one, delete the hyphen. If the image shows a hyphen that the text file has lost, add it.
- The script removes the running header and the page number. Do not add them. Delete a line at the top or the bottom of the page only when the image shows the same line on the page before. Keep a chapter title.
- A word inside a picture, a drawing, or a diagram is not part of the text. Delete it. The image shows you where it is.
- If the text file is empty, write the text that the image shows. If the image shows no text, do not change the file.
- Do not change a page that is correct.
- Do not read other files. Do not run commands.
- Do not write an explanation. Reply with `done`.
