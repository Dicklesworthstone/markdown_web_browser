I have a weird idea for a project called markdown-web-browser

First, read all of https://github.com/allenai/olmocr

My idea is to take a headless chrome, say using playwright or something like that to make it easier, and then basically make a new UI for it like a browser with a URL bar, back and forward buttons, a google search bar, etc. But then it would pass this stuff to the headless chrome, load up the page (including JS and everything so it's all complete), and then save and image of the entire page from the headless chrome instance and seamlessly pass it to the olmocr model to turn into markdown. 

The simplest implementation would use a remote API provider for the ocr model, as shown in the docs:

```
python -m olmocr.pipeline ./localworkspace1 --server https://ai2endpoints.cirrascale.ai/api --api_key sk-XXXXXXX --model olmOCR-2-7B-1025 --pdfs tests/gnarly_pdfs/*.pdf
```

(suitably modified to use image file like png screenshots (or maybe we would convert them to jpeg or webp first to save space, not sure what makes the most sense). Then we'd get back markdown which we would display instead of showing the original HTML. There would be a button in the UI where we could toggle between seeing the raw plaintext markdown with syntax highlighting OR the rendered markdown (rendered using something like the streamdown library). 

Obviously this would be a pretty laggy experience, but we could have a clear progress indicator at the bottom status bar that would convey to the user exactly what's happening:

1) Loading the website and rendering it in chrome
2) Saving the screenshot to file and possibly converting to another format
3) submitting the screenshot to the OCR API service for processing
4) waiting for the response from the API
5) Rendering the response.

Every file processed in this way would be cached in a folder structure where each root URL would get its own folder, and each "page" could get a canonical filename based on the original url and saved as an .md file, and this could be saved in git to deal with the underlying html content changing without losing information. 

Does this make sense? Basically this could be a powerful tool for use by AI agents because they wouldn't need native multi-modal/image/vision processing, just text, but it would still capture the rich semantic content of tables and other complex figures and stuff like that.  