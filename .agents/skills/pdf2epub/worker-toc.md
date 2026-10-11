You select the chapter titles of a book that is already converted. The folder holds a page image `NNNN.png` and a text file `NNNN.md` for each page of the book.

Read `toc.txt` in the folder. It holds one line for each heading block of the book:
```
# page  role      chars  text
0005  body       1274  I. Bölüm
```
- `page` the page number
- `role` the kind of page, or `-` when no one named it
- `chars` the number of characters on the page
- `text` the heading text, in the form of the text file

Select the lines that are chapter titles. Do not add a heading that is not in the list.

Write the selected lines into `chapters.txt` in the folder, in the order of the list:
```
0005 I. Bölüm
0015 II. Bölüm
```
- Write the page number with four digits, one space, then the heading text exactly as `toc.txt` writes it.
- If no line is a chapter title, write an empty file.
- Write no other line. Do not copy the header lines of `toc.txt`.

Rules:
- A chapter title names a part of the text: a number, or a name of its own. Examples: `I. Bölüm`, `-III-`, `BİRİNCİ KISIM`, `Yolculuk`.
- These are not chapter titles: the title of the book, the name of the author, the name of a book series, the name of the publisher, an ISBN, a page number, a line of OCR noise.
- The same heading on many pages is a running header, not a chapter title.
- A heading on a page of the front matter or the back matter of the book is usually not a chapter title. The `role` column shows those pages when it is filled.
- Read the page image `NNNN.png` when the text alone does not decide. The image shows what the page is: the first page of a part of the text, or a page that belongs to the cover, the title, or the imprint of the book.
- Do not read the `.md` files. `toc.txt` holds every heading of the book, and the image shows the rest.
- Do not write an explanation. Reply with `done`.
