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
