<?php

declare(strict_types=1);

// SPC executes this hook against the GPG/hash-verified source after extraction.
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
