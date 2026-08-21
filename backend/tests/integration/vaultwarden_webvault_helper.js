"use strict";

const fs = require("fs");
const { chromium } = require(process.env.VAULTWARDEN_PLAYWRIGHT_MODULE);
let step = "startup";

async function routedPage(browser, origin, upstream) {
  const page = await browser.newPage();
  await page.route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url());
    const target = new URL(upstream);
    url.protocol = target.protocol;
    url.hostname = target.hostname;
    url.port = target.port;
    const response = await route.fetch({ url: url.toString() });
    await route.fulfill({ response });
  });
  return page;
}

async function signup(page, origin, credential) {
  await page.goto(origin, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Create account" }).click();
  await page.getByLabel(/Email address/).fill(credential.email);
  await page.getByLabel("Name").fill("Vaultwarden Drill User");
  await page.getByRole("button", { name: "Continue" }).click();
  await page.waitForURL(/#\/finish-signup/);
  await page
    .getByLabel("Master password* (required)", { exact: true })
    .fill(credential.password);
  await page
    .getByLabel("Confirm master password* (required)", { exact: true })
    .fill(credential.password);
  const breachCheck = page.getByLabel("Check known data breaches for this password");
  if (await breachCheck.isChecked()) {
    await breachCheck.uncheck();
  }
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/#\/setup-extension/);
  await page.getByRole("button", { name: "Add it later" }).click();
  await page.getByRole("link", { name: "Skip to web app" }).click();
  await page.waitForURL(/#\/vault/);
}

async function login(page, origin, credential) {
  step = "login-load";
  await page.goto(origin, { waitUntil: "networkidle" });
  step = "login-email";
  await page.getByTestId("login-email-input").fill(credential.email);
  step = "login-continue";
  await page.getByTestId("login-continue-button").click();
  step = "login-password";
  await page.getByTestId("login-master-password-input").fill(credential.password);
  step = "login-submit";
  await page.getByTestId("login-submit-button").click();
  step = "login-ready";
  await page.waitForURL(/#\/(vault|setup-extension)/);
  if (page.url().includes("#/setup-extension")) {
    await page.getByRole("button", { name: "Add it later" }).click();
    await page.getByRole("link", { name: "Skip to web app" }).click();
    await page.waitForURL(/#\/vault/);
  }
}

async function attach(page, origin, credential) {
  step = "login";
  await login(page, origin, credential);
  step = "open-item";
  await page.goto(
    `${origin}/#/vault?itemId=${encodeURIComponent(credential.item_id)}&action=view`,
    { waitUntil: "networkidle" },
  );
  step = "edit-item";
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  step = "open-attachments";
  await page.getByRole("button", { name: /Attachments/ }).click();
  step = "choose-file";
  await page.locator('input[type="file"]').setInputFiles(credential.file_path);
  step = "upload";
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  step = "upload-confirmation";
  await page.getByText("Attachment saved", { exact: true }).waitFor();
}

async function downloadAttachment(page, origin, credential) {
  step = "download-login";
  await login(page, origin, credential);
  step = "download-open-item";
  await page.goto(
    `${origin}/#/vault?itemId=${encodeURIComponent(credential.item_id)}&action=view`,
    { waitUntil: "networkidle" },
  );
  step = "download-item-name";
  await page.getByText(credential.item_name, { exact: true }).first().waitFor();
  step = "download-note";
  await page.getByText(credential.note, { exact: true }).waitFor();
  step = "download-edit-item";
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  step = "download-open-attachments";
  await page.getByRole("button", { name: /Attachments/ }).click();
  step = "download-file";
  const downloadEvent = page.waitForEvent("download");
  await page
    .getByRole("button", {
      name: `Download ${credential.attachment_name}`,
      exact: true,
    })
    .click();
  const download = await downloadEvent;
  await download.saveAs(credential.output_path);
}

(async () => {
  const credential = JSON.parse(
    fs.readFileSync(process.env.VAULTWARDEN_CREDENTIAL_FILE, "utf8"),
  );
  const browser = await chromium.launch({ headless: true });
  const page = await routedPage(
    browser,
    process.env.VAULTWARDEN_WEB_ORIGIN,
    process.env.VAULTWARDEN_UPSTREAM,
  );
  if (process.env.VAULTWARDEN_WEB_MODE === "signup") {
    await signup(page, process.env.VAULTWARDEN_WEB_ORIGIN, credential);
  } else if (process.env.VAULTWARDEN_WEB_MODE === "attach") {
    await attach(page, process.env.VAULTWARDEN_WEB_ORIGIN, credential);
  } else if (process.env.VAULTWARDEN_WEB_MODE === "download-attachment") {
    await downloadAttachment(page, process.env.VAULTWARDEN_WEB_ORIGIN, credential);
  } else {
    throw new Error("unsupported Web Vault helper mode");
  }
  await browser.close();
  process.stdout.write('{"ok":true}\n');
})().catch((error) => {
  process.stderr.write(`Vaultwarden Web Vault helper failed: ${error.name} (${step})\n`);
  process.exit(1);
});
