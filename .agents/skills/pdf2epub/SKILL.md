---
name: pdf2epub
description: Use when the user asks to convert a PDF book to EPUB, to make an ebook from a PDF, or invokes /pdf2epub.
---

# pdf2epub

A script does the mechanical work. Cheap subagents compare each page image with the page text. You only start commands and subagents.

`SKILL_DIR` is the folder that contains this file. Replace `SKILL_DIR` with the full path in each command and prompt.

## Requirements

Do this before the procedure.

1. Run: `python "SKILL_DIR/scripts/epub.py" check`
   If `python` is not found, try `python3`. Use the name that works in all later commands.
2. If no Python is found, install Python 3.10 or later with the package manager of the system. Then do step 1 again. If the new Python is not found, open a new shell or use the full path of the new Python.
3. For each line that starts with `missing:`, run the command after `install:`. Then do step 1 again.
   If an install command fails, do not try it again. Show the command and its error to the user and stop.
4. Continue when all lines start with `ok:`.

If the user said not to install software, do not install. Show the `missing:` lines to the user and stop. If no Python is found, tell the user that Python is missing.

The OCR program is necessary only for a PDF with no text layer. If the install of the OCR program fails and the PDF has a text layer, go on with the procedure. If that install fails and the PDF has no text layer, stop and show the error to the user.

## Procedure

Do all eight steps. Step 4 is not optional: without it, the EPUB keeps the errors of the PDF text layer.

1. Find the PDF path in the request. Get the title and the author from the request, or from the file name. Omit `--author` when the request and the file name give no author.
2. Run: `python "SKILL_DIR/scripts/epub.py" extract "<pdf>"`
   The output always holds `work:`, `pages:` and `batches:`. Each batch is a page range such as `0001-0010`. It holds `scan:` only when the PDF has pages with no text layer, and `ocr failed:` only when the OCR program fails or is not installed.
   The script writes its progress to the error stream. A line that starts with two spaces is progress, not a result.
   - `scan: N` means that N pages have no text layer. The script read those pages with the OCR program and wrote a draft. The subagents correct the draft from the page image.
   - `ocr failed: ...` means that the text file of those pages is empty. The subagents write those pages from the image.
   A second `extract` on a book that is already converted does no work. Add `--force` when the user asks to convert the book again from the start. The script then deletes every file of the last conversion in the work folder.
3. Start one subagent that names the role of each page. Give it the first twelve page numbers and the last six page numbers of the book, in the same form as a batch.
   Prompt, with no other text: `Read SKILL_DIR/worker-roles.md and follow it. Folder: <work>. Pages: 0001-0012 0253-0258.`
   It writes `roles.txt`. The script uses that file to pick the cover picture and to keep the pages that are not part of the book out of the EPUB (a back cover, an advertisement page, a flap).
   If the subagent fails, go on: the script then uses the first page as the cover and drops nothing.
4. Start one subagent for each batch. Run at most 8 subagents at the same time.
   - Start 8 subagents in one message. Wait until all 8 finish. Then start the next 8.
   - Too many subagents at the same time can exceed the rate limit of the model. The subagents then stop, and they do not continue.
   - Model: a fast model that can read images. If your tool has more than one fast model, use the stronger one, not the smallest one.
   - Prompt, with no other text: `Read SKILL_DIR/worker.md and follow it. Folder: <work>. Pages: <batch>.`
5. If a subagent stops with an error or does not reply `done`, start it again one time. If it fails again, record the batch and continue.
   If a subagent does not finish in 15 minutes, stop it. Then start it again one time.
6. Run: `python "SKILL_DIR/scripts/epub.py" toc "<pdf>" --title "<title>" --author "<author>"`
   The command writes `toc.txt` in the work folder: one line for each heading block of the book, with the page number, the page role, the page length, and the heading text. It prints `candidates:` and the number of lines.
   Then start one subagent that selects the chapter titles from that list.
   Prompt, with no other text: `Read SKILL_DIR/worker-toc.md and follow it. Folder: <work>.`
   It writes `chapters.txt`. Step 7 keeps the headings in that file as chapter titles and turns every other heading into a normal paragraph. Without `chapters.txt`, step 7 keeps every heading.
   An empty `chapters.txt` is a result: it means that the book has no chapter title.
   If the subagent fails, do not write `chapters.txt` yourself. Step 7 then keeps every heading, and the table of contents of the EPUB holds extra lines.
7. Run: `python "SKILL_DIR/scripts/epub.py" build "<pdf>" --title "<title>" --author "<author>"`
   A page in the `flagged pages` list was changed too much by a subagent. The script put the old text back. The version of the subagent is in `<work>/<page>.md.rejected` after this build.
   A `dropped pages` line names the pages that the role subagent did not count as part of the book. The script left them out of the EPUB.
   A `chapters:` line gives the number of chapter titles that stayed in the book. `every heading kept` means that `chapters.txt` is missing.
   An `unmatched chapters:` line names a line of `chapters.txt` that matches no heading. The text of that line is not the text of the heading: the subagent wrote it in the wrong form. Correct that line in `chapters.txt` and run the command again.
8. Give the user the lines of step 7 and the batches that failed. Give the user the page numbers of an `ocr failed:` line too.

If your tool has no subagents, read `SKILL_DIR/worker.md` and do each batch yourself, one batch at a time. Read `SKILL_DIR/worker-toc.md` and select the chapter titles yourself in the same way.

## Rules

- In steps 2 to 7, when you use subagents, do not read the files in the work folder. Do not read `worker.md`, `worker-roles.md`, or `worker-toc.md` either. The subagents read them.
- Do not put the rules of `worker.md` or `worker-toc.md` in the subagent prompt.
- Do not write `chapters.txt` yourself.
- Do not edit the EPUB file.
- If a command stops with an error, show the error line to the user.
- Install only Python and the software that the `check` command names.

## Correct one page

Use this section only when the user reports an error on one page.
1. Find the page: search the `.md` files in the work folder for the words that the user gives.
2. Read `<work>/<page>.png` and `<work>/<page>.md`. The text format is in `SKILL_DIR/worker.md`.
3. Correct the `.md` file of that page only.
4. Run the `build` command again.
