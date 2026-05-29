/* Real Chromium + temporary Python fixture. All inference/Google transports stay synthetic.
   Install playwright as a dev tool or set PLAYWRIGHT_MODULE_PATH to an existing package. */
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
