<?php

declare(strict_types=1);

// SPC executes this hook against the GPG/hash-verified source after extraction.
if ($this->getPatchPoint() === 'before-sanity-check' && $this->getPHPVersionID() < 80000 && PHP_OS_FAMILY === 'Linux') {
    // Shared-extension builds also test an embed library. SPC 2.8.5 copies
    // archive headings/unquoted names from nm into GNU ld's dynamic list.
    // Parse only defined symbol records and quote their names; keep the test.
    $archive = BUILD_LIB_PATH . '/libphp.a';
    $rows = [];
    exec('nm -g --defined-only -P ' . escapeshellarg($archive), $rows, $status);
    if ($status !== 0) {
        throw new RuntimeException('Could not read the PHP embed symbol table');
    }
    $symbols = [];
    foreach ($rows as $row) {
        if (preg_match('/^(\S+)\s+[A-Za-z]\s+[0-9a-fA-F]+(?:\s+[0-9a-fA-F]+)?$/', trim($row), $match)) {
            $symbols[] = '  "' . addcslashes(explode('@', $match[1])[0], "\\\"") . '";';
        }
    }
    $symbols = array_unique($symbols);
    if (!$symbols) {
        throw new RuntimeException('PHP embed symbol table has no defined exports');
    }
    sort($symbols);
    $list = "{\n" . implode("\n", $symbols) . "\n};\n";
    if (file_put_contents($archive . '.dynsym', $list) !== strlen($list)) {
        throw new RuntimeException('Could not write the PHP embed symbol list');
    }
    return;
}

if ($this->getPatchPoint() !== 'after-php-extract' || $this->getPHPVersionID() >= 80100) {
    return;
}

// libxml2 removed its public ATTRIBUTE_UNUSED macro. PHP 7.4/8.0 already
// provides the equivalent Zend macro; keep the annotation without depending
// on libxml2's internal headers. This changes no runtime behavior.
$file = SOURCE_PATH . '/php-src/ext/libxml/libxml.c';
$source = file_get_contents($file);
$before = 'int compression ATTRIBUTE_UNUSED)';
if ($source === false || substr_count($source, $before) !== 1) {
    throw new RuntimeException('Unexpected PHP libxml source; review the build patch');
}
$patched = str_replace($before, 'int compression ZEND_ATTRIBUTE_UNUSED)', $source);
if (file_put_contents($file, $patched) !== strlen($patched)) {
    throw new RuntimeException('Could not write the PHP libxml build patch');
}

// Backport the ICU language-standard check from PHP 8.1.34. ICU 74+ headers
// require C++17; PHP 7.4/8.0 otherwise force C++11 even with a newer compiler.
$file = SOURCE_PATH . '/php-src/ext/intl/config.m4';
$source = file_get_contents($file);
$before = 'PHP_CXX_COMPILE_STDCXX(11, mandatory, PHP_INTL_STDCXX)';
$after = <<<'M4'
AS_IF([$PKG_CONFIG icu-uc --atleast-version=74],[
    PHP_CXX_COMPILE_STDCXX(17, mandatory, PHP_INTL_STDCXX)
  ],[
    PHP_CXX_COMPILE_STDCXX(11, mandatory, PHP_INTL_STDCXX)
  ])
M4;
if ($source === false || substr_count($source, $before) !== 1) {
    throw new RuntimeException('Unexpected PHP intl source; review the build patch');
}
$patched = str_replace($before, $after, $source);
if (file_put_contents($file, $patched) !== strlen($patched)) {
    throw new RuntimeException('Could not write the PHP intl build patch');
}
