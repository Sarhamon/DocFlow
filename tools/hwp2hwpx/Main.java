import kr.dogfoot.hwp2hwpx.Hwp2Hwpx;
import kr.dogfoot.hwplib.reader.HWPReader;
import kr.dogfoot.hwpxlib.writer.HWPXWriter;

/** HWP 하나를 HWPX 로: java Main <원본.hwp> <대상.hwpx> (docflow/adapters/hwp2hwpx.py 가 부른다) */
public class Main {
    public static void main(String[] args) throws Exception {
        HWPXWriter.toFilepath(Hwp2Hwpx.toHWPX(HWPReader.fromFile(args[0])), args[1]);
    }
}
