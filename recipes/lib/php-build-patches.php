<?php

declare(strict_types=1);

// SPC executes this hook against the GPG/hash-verified source after extraction.
if ($this->getPatchPoint() !== 'after-php-extract' || $this->getPHPVersionID() >= 80100) {
    return;
}

$replace = static function (string $relative, string $before, string $after, int $count = 1): void {
    $file = SOURCE_PATH . '/php-src/' . $relative;
    $source = file_get_contents($file);
    if ($source === false || substr_count($source, $before) !== $count) {
        throw new RuntimeException('Unexpected PHP source in ' . $relative . '; review the build patch');
    }
    $patched = str_replace($before, $after, $source);
    if (file_put_contents($file, $patched) !== strlen($patched)) {
        throw new RuntimeException('Could not write the PHP build patch for ' . $relative);
    }
};

if ($this->getPHPVersionID() < 80000) {
    // PHP 7 calls its embed library libphp7, while SPC's install/export/test
    // pipeline expects libphp. Match PHP 8's build names so SPC inspects the
    // actual archive instead of creating an empty libphp.a beside libphp7.a.
    $replace('configure.ac', 'libphp[]$PHP_MAJOR_VERSION', 'libphp', 3);
    $replace('build/php.m4', 'libphp[]$PHP_MAJOR_VERSION', 'libphp', 3);
    $replace('build/Makefile.global', 'libphp$(PHP_MAJOR_VERSION)', 'libphp', 9);
}

// libxml2 removed its public ATTRIBUTE_UNUSED macro. PHP 7.4/8.0 already
// provides the equivalent Zend macro; keep the annotation without depending
// on libxml2's internal headers. This changes no runtime behavior.
$replace('ext/libxml/libxml.c', 'int compression ATTRIBUTE_UNUSED)', 'int compression ZEND_ATTRIBUTE_UNUSED)');

// Backport the ICU language-standard check from PHP 8.1.34. ICU 74+ headers
// require C++17; PHP 7.4/8.0 otherwise force C++11 even with a newer compiler.
$before = 'PHP_CXX_COMPILE_STDCXX(11, mandatory, PHP_INTL_STDCXX)';
$after = <<<'M4'
AS_IF([$PKG_CONFIG icu-uc --atleast-version=74],[
    PHP_CXX_COMPILE_STDCXX(17, mandatory, PHP_INTL_STDCXX)
  ],[
    PHP_CXX_COMPILE_STDCXX(11, mandatory, PHP_INTL_STDCXX)
  ])
M4;
$replace('ext/intl/config.m4', $before, $after);

// Upstream GH-16348 fixes clang merging non-local inline-assembly labels,
// which makes PHP <=8.0 abort at startup on Intel macOS. PHP 8.1.34 already
// includes this fix: https://github.com/php/php-src/commit/806d2e073c1fe67dfe3c5791f4483f44dd991b28
passthru('patch -f -F 0 -p1 -d ' . escapeshellarg(SOURCE_PATH . '/php-src')
    . ' -i ' . escapeshellarg(__DIR__ . '/php-zend-local-labels.patch'), $status);
if ($status !== 0) {
    throw new RuntimeException('Could not apply the upstream Zend local-label fix');
}
