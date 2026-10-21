#!/usr/bin/env node
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const SKILL_NAME = 'designing-databases';
const PKG_ROOT = path.resolve(__dirname, '..');
const PAYLOAD = ['SKILL.md', 'references', 'scripts', 'assets'];

function usage() {
  console.log(`designing-databases — install the Agent Skill into a skills directory

Usage:
  npx @vstlmkh/designing-databases install [options]
  npx @vstlmkh/designing-databases uninstall [options]
  npx @vstlmkh/designing-databases where [options]

Options:
  --user          install into ~/.claude/skills (default)
  --project       install into ./.claude/skills
  --dir <path>    install into <path>/${SKILL_NAME}
  --force         overwrite an existing installation
  -h, --help      show this help
`);
}

function parseArgs(argv) {
  const opts = { command: null, scope: 'user', dir: null, force: false, help: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '-h' || a === '--help') opts.help = true;
    else if (a === '--user') opts.scope = 'user';
    else if (a === '--project') opts.scope = 'project';
    else if (a === '--force') opts.force = true;
    else if (a === '--dir') {
      opts.dir = argv[++i];
      if (!opts.dir) throw new Error('--dir requires a path');
    } else if (a.startsWith('-')) throw new Error(`unknown option: ${a}`);
    else if (!opts.command) opts.command = a;
    else throw new Error(`unexpected argument: ${a}`);
  }
  return opts;
}

function skillsRoot(opts) {
  if (opts.dir) return path.resolve(opts.dir);
  if (opts.scope === 'project') return path.resolve(process.cwd(), '.claude', 'skills');
  return path.join(os.homedir(), '.claude', 'skills');
}

function copy(src, dest) {
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      if (entry === '__pycache__' || entry.endsWith('.pyc')) continue;
      copy(path.join(src, entry), path.join(dest, entry));
    }
  } else {
    fs.copyFileSync(src, dest);
  }
}

function install(opts) {
  const target = path.join(skillsRoot(opts), SKILL_NAME);
  if (fs.existsSync(target) && !opts.force) {
    console.error(`${target} already exists. Re-run with --force to overwrite.`);
    process.exit(1);
  }
  if (fs.existsSync(target)) fs.rmSync(target, { recursive: true, force: true });
  fs.mkdirSync(target, { recursive: true });
  for (const item of PAYLOAD) {
    const src = path.join(PKG_ROOT, item);
    if (fs.existsSync(src)) copy(src, path.join(target, item));
  }
  console.log(`Installed ${SKILL_NAME} to ${target}`);
  console.log('Restart your agent session to pick the skill up.');
}

function uninstall(opts) {
  const target = path.join(skillsRoot(opts), SKILL_NAME);
  if (!fs.existsSync(target)) {
    console.log(`Nothing to remove at ${target}`);
    return;
  }
  fs.rmSync(target, { recursive: true, force: true });
  console.log(`Removed ${target}`);
}

function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(String(err.message));
    usage();
    process.exit(2);
  }
  if (opts.help || !opts.command) return usage();

  switch (opts.command) {
    case 'install': return install(opts);
    case 'uninstall':
    case 'remove': return uninstall(opts);
    case 'where':
      console.log(path.join(skillsRoot(opts), SKILL_NAME));
      return;
    default:
      console.error(`unknown command: ${opts.command}`);
      usage();
      process.exit(2);
  }
}

main();
