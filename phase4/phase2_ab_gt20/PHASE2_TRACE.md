# Phase 2 GT-20 trigger trace

Built from Phase-2-OFF extraction + recovery policy/payload inspection.
Does not call Gemini.

| document_id | accepted | failure | strategy | gemini | OCR evidence | failing in evidence |
|---|---|---|---|---|---|---|
| 0000700084_2026_08_ZL_06_8236_03092026155136.pdf | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 3 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700124_2026_08_ZL_06_521_02092026204703.pdf | False | EMPTY_OUTPUT | structured_reread | would_call | True | 0 |

- reason: `failure=EMPTY_OUTPUT strategy=structured_reread`
- validation: `['empty_line_items', 'low_ocr_quality_score']`
| 0000700209_2026_08_ZL_18_374_04092026060313.pdf | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 5 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700275_2026_08_ZL_18_374_04092026171615.pdf | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 15 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700308_2026_08_ZL_18_8175_03092026072320.jpg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 1 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700317_2026_08_ZL_18_374_02092026171443.pdf | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 10 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700346_2026_08_ZL_18_8189_05092026025159.pdf | False | EMPTY_OUTPUT | structured_reread | would_call | True | 0 |

- reason: `failure=EMPTY_OUTPUT strategy=structured_reread`
- validation: `['empty_line_items', 'low_ocr_quality_score']`
| 0000700358_2026_08_ZL_18_8173_05092026185826.pdf | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 10 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700364_2026_08_ZL_18_8173_05092026190156.pdf | True | None | None | not_needed | None | None |

- reason: `validation_accepted_or_unclassified`
| 0000700406_2026_08_ZL_10_384_05092026140711.jpg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 20 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure', 'missing_product_names']`
| 0000700434_2026_08_ZL_10_386_05092026034503.jpg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 2 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000700835_2026_08_ZL_25_447_03092026105819.jpeg | True | None | None | not_needed | None | None |

- reason: `validation_accepted_or_unclassified`
| 0000701012_2026_08_ZL_11_423_03092026183736.jpg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 33 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000701078_2026_08_ZL_03_362_05092026080346.jpeg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 3 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000701151_2026_08_ZL_14_361_03092026025650.jpg | True | None | None | not_needed | None | None |

- reason: `validation_accepted_or_unclassified`
| 0000701275_2026_08_ZL_03_360_02092026152716.jpg | False | STOCK_IDENTITY_FAILURE | targeted_stock_extraction | would_call | True | 20 |

- reason: `failure=STOCK_IDENTITY_FAILURE strategy=targeted_stock_extraction`
- validation: `['stock_identity_failure']`
| 0000701851_2026_08_ZL_20_302_03092026134940.jpg | True | None | None | not_needed | None | None |

- reason: `validation_accepted_or_unclassified`
| 0000701864_2026_08_ZL_20_5015_03092026091037.jpg | True | None | None | not_needed | None | None |

- reason: `validation_accepted_or_unclassified`
| 0000701904_2026_08_ZL_20_8227_05092026154848.jpg | False | EMPTY_OUTPUT | structured_reread | would_call | True | 0 |

- reason: `failure=EMPTY_OUTPUT strategy=structured_reread`
- validation: `['empty_line_items', 'low_ocr_quality_score']`
| 0000704337_2026_08_ZL_24_259_03092026042213.pdf | False | EMPTY_OUTPUT | structured_reread | would_call | True | 0 |

- reason: `failure=EMPTY_OUTPUT strategy=structured_reread`
- validation: `['empty_line_items', 'low_ocr_quality_score']`
