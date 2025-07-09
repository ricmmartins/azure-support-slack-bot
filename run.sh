case $@ in
    lint|l)
        flake8 .
        ;;
    lint-in-place|lip)
        autopep8 --in-place --aggressive -r .
        ;;
  *)
    # echo "Command \"$@\" doesn't exist. Typo?"
    ;;
esac
